<?php
/**
 * Plugin Name: Unicornio E2E Fault Injection
 * Description: Ephemeral-only REST fault injection and request audit for CI.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

function unicornio_e2e_fault_option( $post_id ) {
	return 'unicornio_e2e_fault_' . absint( $post_id );
}

function unicornio_e2e_audit_option( $post_id ) {
	return 'unicornio_e2e_audit_' . absint( $post_id );
}

function unicornio_e2e_audit_event( $post_id, $event ) {
	$key   = unicornio_e2e_audit_option( $post_id );
	$events = get_option( $key, array() );
	if ( ! is_array( $events ) ) {
		$events = array();
	}
	$events[] = array_merge(
		array( 'at' => gmdate( 'c' ) ),
		$event
	);
	update_option( $key, array_slice( $events, -200 ), false );
}

add_action( 'rest_api_init', function () {
	register_rest_route( 'unicornio-e2e/v1', '/fault', array(
		'methods'             => 'POST',
		'permission_callback' => function () { return current_user_can( 'manage_options' ); },
		'callback'            => function ( WP_REST_Request $request ) {
			$post_id = absint( $request->get_param( 'post_id' ) );
			$mode    = sanitize_key( $request->get_param( 'mode' ) );
			$allowed = array( 'fail_status_post', 'interrupt_next_get', 'divergent_readback' );
			if ( ! $post_id || ! in_array( $mode, $allowed, true ) ) {
				return new WP_Error( 'invalid_e2e_fault', 'Invalid test fault.', array( 'status' => 400 ) );
			}
			update_option( unicornio_e2e_fault_option( $post_id ), array( 'mode' => $mode, 'stage' => 'armed' ), false );
			return rest_ensure_response( array( 'armed' => true, 'post_id' => $post_id, 'mode' => $mode ) );
		},
	) );

	register_rest_route( 'unicornio-e2e/v1', '/audit', array(
		'methods'             => 'GET',
		'permission_callback' => function () { return current_user_can( 'manage_options' ); },
		'callback'            => function ( WP_REST_Request $request ) {
			$post_id = absint( $request->get_param( 'post_id' ) );
			return rest_ensure_response( array(
				'post_id' => $post_id,
				'events'  => get_option( unicornio_e2e_audit_option( $post_id ), array() ),
			) );
		},
	) );
} );

add_filter( 'rest_pre_dispatch', function ( $result, $server, $request ) {
	$route = $request->get_route();
	if ( ! preg_match( '#^/wp/v2/posts/(\d+)$#', $route, $matches ) ) {
		return $result;
	}
	$post_id = absint( $matches[1] );
	$method  = strtoupper( $request->get_method() );
	$body    = $request->get_json_params();
	$body    = is_array( $body ) ? $body : array();
	$fault   = get_option( unicornio_e2e_fault_option( $post_id ), array() );
	unicornio_e2e_audit_event( $post_id, array(
		'phase' => 'request', 'method' => $method, 'route' => $route,
		'requested_status' => isset( $body['status'] ) ? sanitize_key( $body['status'] ) : '',
	) );

	if ( $method === 'POST' && ( $body['status'] ?? '' ) === 'awaiting_human' && ( $fault['mode'] ?? '' ) === 'fail_status_post' ) {
		delete_option( unicornio_e2e_fault_option( $post_id ) );
		unicornio_e2e_audit_event( $post_id, array( 'phase' => 'fault', 'mode' => 'fail_status_post', 'http_status' => 503 ) );
		return new WP_Error( 'unicornio_e2e_controlled_failure', 'Controlled E2E status write failure.', array( 'status' => 503 ) );
	}

	if ( $method === 'GET' && ( $fault['stage'] ?? '' ) === 'after_status_post' && ( $fault['mode'] ?? '' ) === 'interrupt_next_get' ) {
		delete_option( unicornio_e2e_fault_option( $post_id ) );
		unicornio_e2e_audit_event( $post_id, array( 'phase' => 'fault', 'mode' => 'interrupt_next_get', 'http_status' => 503 ) );
		return new WP_Error( 'unicornio_e2e_readback_interrupted', 'Controlled E2E readback interruption.', array( 'status' => 503 ) );
	}

	return $result;
}, 10, 3 );

add_action( 'rest_after_insert_post', function ( $post, $request, $creating ) {
	if ( $creating || ! $post instanceof WP_Post || $post->post_status !== 'awaiting_human' ) {
		return;
	}
	$post_id = absint( $post->ID );
	$fault   = get_option( unicornio_e2e_fault_option( $post_id ), array() );
	if ( ( $fault['stage'] ?? '' ) === 'armed' && in_array( $fault['mode'] ?? '', array( 'interrupt_next_get', 'divergent_readback' ), true ) ) {
		$fault['stage'] = 'after_status_post';
		update_option( unicornio_e2e_fault_option( $post_id ), $fault, false );
	}
}, 10, 3 );

add_filter( 'rest_post_dispatch', function ( $response, $server, $request ) {
	$route = $request->get_route();
	if ( ! preg_match( '#^/wp/v2/posts/(\d+)$#', $route, $matches ) ) {
		return $response;
	}
	$post_id = absint( $matches[1] );
	$method  = strtoupper( $request->get_method() );
	$status  = is_wp_error( $response ) ? 500 : ( $response instanceof WP_REST_Response ? $response->get_status() : 200 );
	$fault   = get_option( unicornio_e2e_fault_option( $post_id ), array() );
	if ( $method === 'GET' && ( $fault['stage'] ?? '' ) === 'after_status_post' && ( $fault['mode'] ?? '' ) === 'divergent_readback' && $response instanceof WP_REST_Response ) {
		$data = $response->get_data();
		if ( is_array( $data ) ) {
			$data['status'] = 'pending';
			$response->set_data( $data );
		}
		delete_option( unicornio_e2e_fault_option( $post_id ) );
		unicornio_e2e_audit_event( $post_id, array(
			'phase' => 'fault', 'mode' => 'divergent_readback', 'http_status' => $status,
			'api_reported_status' => 'pending', 'wordpress_stored_status' => get_post_status( $post_id ),
		) );
	}
	$response_data = $response instanceof WP_REST_Response ? $response->get_data() : array();
	unicornio_e2e_audit_event( $post_id, array(
		'phase' => 'response', 'method' => $method, 'route' => $route, 'http_status' => $status,
		'api_reported_status' => $method === 'GET' && is_array( $response_data ) ? sanitize_key( $response_data['status'] ?? '' ) : '',
	) );
	return $response;
}, 10, 3 );
