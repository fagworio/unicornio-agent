<?php
/**
 * Plugin Name: Unicornio REST Meta
 * Description: Expõe campos customizados usados pelo agente editorial na REST API
 *              (original_link, metas Rank Math de SEO e o estado operacional
 *              _hermes_* / _ai_editor_*). Sem isso, a REST não devolve nem grava
 *              post meta não registrada — o pipeline de estados (READY/BLOCKED/
 *              AWAITING_HUMAN, ready_hash, attempts, next_retry_at) ficaria só
 *              no filesystem e o backoff/rework não escalaria.
 * Version: 0.3.0
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

add_action( 'init', function () {
	// Campos legados de SEO/fonte (0.1/0.2) + estado operacional do pipeline (0.3).
	$meta_fields = array(
		'original_link'              => 'sanitize_text_field',
		'rank_math_title'            => 'sanitize_text_field',
		'rank_math_description'      => 'sanitize_text_field',
		'rank_math_focus_keyword'    => 'sanitize_text_field',
		// Estado operacional (fonte de verdade do queue/monitor/publish).
		'_hermes_state'              => 'sanitize_text_field',
		'_hermes_attempts'           => 'sanitize_text_field',
		'_hermes_next_retry_at'      => 'sanitize_text_field',
		'_hermes_last_error'         => 'sanitize_text_field',
		'_hermes_ready_hash'         => 'sanitize_text_field',
		'_hermes_ready_manifest'     => 'strval', // JSON canônico compacto
		'_hermes_policy_version'     => 'sanitize_text_field',
		'_hermes_processed_at'       => 'sanitize_text_field',
		// Marcadores legados de auditoria do apply/publish.
		'_ai_editor_version'         => 'sanitize_text_field',
		'_ai_editor_decision'        => 'sanitize_text_field',
		'_ai_editor_confidence'      => 'sanitize_text_field',
		'_ai_editor_processed_at'    => 'sanitize_text_field',
		'_ai_editor_correlation_id'  => 'sanitize_text_field',
		'_ai_editor_published_at'    => 'sanitize_text_field',
	);

	foreach ( $meta_fields as $key => $sanitize ) {
		register_post_meta( 'post', $key, array(
			'type'              => 'string',
			'single'            => true,
			'show_in_rest'      => true,
			'sanitize_callback' => $sanitize,
			// Garante que o usuario de automacao (redacao-agent) possa GRAVAR
			// estes campos via REST: sem auth_callback explicito o core do WP
			// pode negar com rest_cannot_update mesmo para quem edita o post.
			'auth_callback'     => '__return_true',
		) );
	}
} );
