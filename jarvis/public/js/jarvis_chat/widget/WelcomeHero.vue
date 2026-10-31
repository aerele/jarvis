<template>
	<!-- The welcome's hero tile. Its look (size, gradient) is Panel's .jvp-hero, which the setup
	     nudge shares; this part holds only what the holiday adds: the smile, the worn item and
	     the replay control. In the upgrade hold the parent supplies the sleeping face. -->
	<div
		class="jvp-hero"
		:class="{ 'jvp-sleeping': sleeping, 'jvp-hero--button': clickable }"
		:role="clickable ? 'button' : undefined"
		:tabindex="clickable ? 0 : undefined"
		:aria-label="clickable ? 'Replay the holiday greeting' : undefined"
		@click="replay"
		@keydown.enter.prevent="replay"
		@keydown.space.prevent="replay"
	>
		<slot v-if="sleeping" name="sleeping"></slot>
		<svg
			v-else-if="!smiling"
			class="jvp-star"
			viewBox="0 0 24 24"
			fill="#fff"
			aria-hidden="true"
		>
			<path d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z" />
		</svg>
		<!-- A holiday smile: the mark's happy face (JarvisMark's happy mood). -->
		<span v-if="smiling && !sleeping" class="jvp-smile" aria-hidden="true">
			<span class="jvp-smile-eyes"><i></i><i></i></span>
			<span class="jvp-smile-mouth"></span>
		</span>
		<!-- v-html is safe: built by the art from constants and a design key the answer reader has
		     already checked. -->
		<svg
			v-if="itemMarkup"
			class="jvh-layer jvp-item"
			:data-design="design"
			data-state="happy"
			data-testid="holiday-hero-worn"
			viewBox="-16 -16 88 88"
			:style="layerBox"
			focusable="false"
			aria-hidden="true"
			v-html="itemMarkup"
		></svg>
	</div>
</template>

<script setup>
const props = defineProps({
	sleeping: { type: Boolean, default: false },
	smiling: { type: Boolean, default: false },
	clickable: { type: Boolean, default: false },
	design: { type: String, default: "" },
	itemMarkup: { type: String, default: "" },
	layerBox: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["replay"]);

function replay() {
	if (props.clickable) emit("replay");
}
</script>

<style scoped>
/* The star the hero rests on (Panel's .jvp-hero svg sizing does not reach inside this component). */
.jvp-star {
	width: 29px;
	height: 29px;
}
.jvp-hero--button {
	cursor: pointer;
}
.jvp-hero--button:focus-visible {
	outline: 2px solid var(--jv-accent);
	outline-offset: 3px;
}
.jvp-smile {
	position: absolute;
	inset: 0;
	display: flex;
	flex-direction: column;
	align-items: center;
	justify-content: center;
	gap: calc(var(--sz) * 0.07);
	animation: jvp-cheer 1.5s ease-in-out 1 both;
}
.jvp-smile-eyes {
	display: flex;
	gap: calc(var(--sz) * 0.05);
}
.jvp-smile-eyes i {
	width: calc(var(--sz) * 0.1);
	height: calc(var(--sz) * 0.14);
	background: #fff;
	border-radius: 999px;
}
.jvp-smile-mouth {
	width: calc(var(--sz) * 0.42);
	height: calc(var(--sz) * 0.34);
	border: calc(var(--sz) * 0.07) solid #fff;
	border-color: transparent transparent #fff transparent;
	border-radius: 50%;
	margin-top: calc(var(--sz) * -0.1);
}
@keyframes jvp-cheer {
	0%,
	100% {
		transform: translateY(3%) scale(1);
	}
	50% {
		transform: translateY(-12%) scale(1.05);
	}
}
.jvp-item {
	position: absolute;
	overflow: visible;
	pointer-events: none;
}
.jvp-item :deep(.acc) {
	animation: jvp-item-pop 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) both;
}
@keyframes jvp-item-pop {
	from {
		opacity: 0;
		transform: translateY(-5px) scale(0.86);
	}
}
@media (prefers-reduced-motion: reduce) {
	.jvp-smile,
	.jvp-item :deep(.acc) {
		animation: none;
	}
}
</style>
