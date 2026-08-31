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

// Saída do status awaiting_human -> pending (manual no WP ou via `retry`
// do pipeline) reseta o estado operacional: o post volta a ser trilhado
// como novo (NEW), sem tentativas/cooldown herdados. Entrada para
// awaiting_human NÃO limpa nada (o apply acabou de gravar as metas).
add_action( 'transition_post_status', function ( $new_status, $old_status, $post ) {
	if ( $post->post_type !== 'post' ) {
		return;
	}
	if ( $new_status === 'pending' && $old_status === 'awaiting_human' ) {
		foreach ( array(
			'_hermes_state',
			'_hermes_attempts',
			'_hermes_next_retry_at',
			'_hermes_last_error',
			'_hermes_processed_at',
		) as $key ) {
			delete_post_meta( $post->ID, $key );
		}
	}
}, 10, 3 );

// Disponibiliza "Awaiting Human" no Quick Edit e Bulk Edit da listagem de
// posts (filtro oficial do core desde 6.9) — atribuição manual sem abrir
// o editor.
add_filter( 'quick_edit_statuses', function ( $statuses, $post_type, $bulk, $can_publish ) {
	if ( $post_type === 'post' ) {
		$statuses['awaiting_human'] = 'Awaiting Human';
	}
	return $statuses;
}, 10, 4 );

// Atalho no menu lateral: Posts -> Awaiting Human (filtro direto), sempre
// visível mesmo sem posts no status.
add_action( 'admin_menu', function () {
	add_submenu_page(
		'edit.php',
		'Posts Awaiting Human',
		'Awaiting Human',
		'edit_posts',
		'edit.php?post_status=awaiting_human'
	);
} );

// Editor CLASSICO: o select de status e o rotulo sao hardcoded no core
// (so os 4-5 builtin). Este script adiciona a opcao "Awaiting Human" ao
// select e corrige o rotulo do status atual quando o post esta nele.
add_action( 'admin_footer', function () {
	$screen = get_current_screen();
	if ( ! $screen || $screen->base !== 'post' || $screen->post_type !== 'post' ) {
		return;
	}
	$is_current = get_post_status() === 'awaiting_human';
	?>
	<script>
	(function () {
		var sel = document.getElementById('post_status');
		if (!sel) { return; }
		var opt = document.createElement('option');
		opt.value = 'awaiting_human';
		opt.textContent = 'Awaiting Human';
		<?php if ( $is_current ) { ?>opt.selected = true;<?php } ?>
		sel.appendChild(opt);
		<?php if ( $is_current ) { ?>
		var disp = document.getElementById('post-status-display');
		if (disp) { disp.textContent = 'Awaiting Human'; }
		<?php } ?>
	})();
	</script>
	<?php
}, 99 );
