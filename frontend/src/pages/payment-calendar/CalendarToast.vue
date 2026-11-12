<template>
	<Teleport to="body">
		<div class="pcal-overlay pcal-toast" role="status" :hidden="!message">{{ message }}</div>
	</Teleport>
</template>
<script setup>
import { onBeforeUnmount, watch } from "vue";
import "./tokens.css";
import "./CalendarToast.css";

const props = defineProps({ message: { type: String, default: "" } });
const emit = defineEmits(["hidden"]);

const VISIBLE_MS = 3500;
let timer = null;
const stop = () => clearTimeout(timer);

// A new message restarts the countdown; the page clears the message on "hidden".
watch(
	() => props.message,
	(message) => {
		stop();
		if (message) timer = setTimeout(() => emit("hidden"), VISIBLE_MS);
	},
	{ immediate: true }
);
onBeforeUnmount(stop);
</script>
