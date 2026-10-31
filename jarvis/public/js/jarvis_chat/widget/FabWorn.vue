<template>
	<!-- The item the launcher wears on a holiday. A SIBLING of the launcher tile, not a child:
	     the tile clips (overflow: hidden, rounded), and a hat or umbrella hangs past its top.
	     It carries the tile's own drag/snap transform and fade, so it travels with it. -->
	<div
		class="jvw-worn"
		:class="{ 'jvw-worn--snapping': snapping, 'jvw-worn--faded': faded }"
		:style="{ transform }"
		aria-hidden="true"
	>
		<Transition name="jvw-item">
			<!-- v-html is safe: the markup is built by the art from constants and a design key the
			     answer reader has already checked against its own list. -->
			<svg
				v-if="shown"
				class="jvh-layer"
				:class="{ 'jvw-item--pop': motion }"
				:data-design="design"
				data-state="peek"
				data-testid="holiday-fab-worn"
				viewBox="-16 -16 88 88"
				:style="box"
				focusable="false"
				v-html="markup"
			></svg>
		</Transition>
	</div>
</template>

<script setup>
import { computed } from "vue";
import { FAB_PX, FAB_RADIUS_PX, wornLayerBox } from "./fab_worn.mjs";
import { tileUnits } from "../../shared/holiday/worn-room.mjs";

const props = defineProps({
	art: { type: Object, required: true }, // window.__jarvisHolidayArt
	design: { type: String, required: true },
	shown: { type: Boolean, default: false },
	x: { type: Number, default: 0 },
	y: { type: Number, default: 0 },
	snapping: { type: Boolean, default: false },
	faded: { type: Boolean, default: false },
	motion: { type: Boolean, default: true },
});

const box = wornLayerBox();
const transform = computed(() => `translate3d(${props.x}px, ${props.y}px, 0)`);
// The launcher's corner radius in the art's tile space.
const markup = computed(() =>
	props.art.wornMarkup(props.design, {
		idPrefix: "jvwf",
		radius: tileUnits(FAB_RADIUS_PX, FAB_PX),
		motion: props.motion,
	})
);
</script>

<style scoped>
.jvw-worn {
	position: fixed;
	left: 0;
	top: 0;
	width: 54px;
	height: 54px;
	z-index: 1122; /* just over the launcher tile's 1121 */
	pointer-events: none;
	will-change: transform;
	transition: opacity 0.25s ease;
}
.jvw-worn--snapping {
	transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.25s ease;
}
.jvw-worn--faded {
	opacity: 0.4;
}
.jvh-layer {
	position: absolute;
	overflow: visible;
	pointer-events: none;
}
/* The item fades on with the face; for a hover or peek it also pops. */
.jvw-item-enter-active {
	animation: jvw-item-fade 0.18s ease-out both;
}
.jvw-item-enter-active.jvw-item--pop :deep(.acc) {
	animation: jvw-item-pop 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) both;
}
.jvw-item-leave-active {
	transition: opacity 0.2s ease;
}
.jvw-item-leave-to {
	opacity: 0;
}
@keyframes jvw-item-fade {
	from {
		opacity: 0;
	}
}
@keyframes jvw-item-pop {
	from {
		opacity: 0;
		transform: translateY(-5px) scale(0.86);
	}
}
</style>
