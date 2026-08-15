<script setup>
import { store } from "../store";

// Every drawer destination gets the same bar, with the hamburger in the same
// place. Previously only the chat list had one, so from any other screen the
// drawer was simply unreachable — which is most of why it felt broken.
defineProps({ title: { type: String, required: true } });
</script>

<template>
	<div class="jv-bar">
		<button class="jv-icon-btn" aria-label="Menu" @click="store.drawerOpen = true">
			<svg
				viewBox="0 0 24 24"
				width="20"
				height="20"
				fill="none"
				stroke="currentColor"
				stroke-width="1.8"
				stroke-linecap="round"
			>
				<path d="M3 6h18M3 12h18M3 18h18" />
			</svg>
		</button>
		<div class="jv-title">{{ title }}</div>
		<slot name="actions" />
	</div>
</template>
