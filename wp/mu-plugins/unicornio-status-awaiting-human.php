<?php
/**
 * Plugin Name: Unicornio Status Awaiting Human
 * Description: Registra o status customizado "awaiting_human" para posts que o
 *              agente editorial esgotou (imagens/destaque/visao) e precisam de
 *              decisão humana. Aparece como filtro na tela "Todos os Posts" e
 *              é aceito pela REST API (mover: POST /wp/v2/posts/{id} com
 *              status=awaiting_human). O pipeline nunca publica posts nesse
 *              status; ele só volta ao fluxo via "retry" (status=pending).
 * Version: 1.0.0
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

add_action( 'init', function () {
	// Segue o padrão do status nativo 'pending': não é público, não aparece em
	// buscas, mas é listável/atribuível na administração. show_in_admin_all_list
	// inclui na listagem "Todos" e show_in_admin_status_list adiciona o filtro
	// no dropdown de status da tela de posts.
	register_post_status( 'awaiting_human', array(
		'label'                     => 'Awaiting Human',
		'label_count'               => _n_noop( 'Awaiting Human (%s)', 'Awaiting Human (%s)' ),
		'public'                    => false,
		'internal'                  => false,
		'protected'                 => false,
		'private'                   => false,
		'publicly_queryable'        => false,
		'exclude_from_search'       => true,
		'show_in_admin_all_list'    => true,
		'show_in_admin_status_list' => true,
		'date_floating'             => false,
	) );
} );

// Mostra o rótulo "Awaiting Human" ao lado do título na listagem de posts
// (igual aos estados nativos: Pendente, Rascunho, Privado...).
add_filter( 'display_post_states', function ( $states, $post ) {
	if ( get_post_status( $post ) === 'awaiting_human' ) {
		$states['awaiting_human'] = 'Awaiting Human';
	}
	return $states;
}, 10, 2 );
