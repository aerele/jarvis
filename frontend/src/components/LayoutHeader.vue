<template>
	<Teleport v-if="showHeader" to="#app-header">
		<slot>
			<header class="flex h-10.5 items-center justify-between py-[7px] sm:pl-5 pl-2 pr-5">
				<div class="flex items-center gap-2">
					<slot name="left-header" />
				</div>
				<div class="flex items-center gap-2">
					<!-- "Go to Desk" - uniform across every page, and always the
					     LEFTMOST item of the right cluster so each page's primary
					     action keeps the rightmost corner (the design standard). -->
					<Button
						variant="outline"
						size="sm"
						icon="external-link"
						label="Open ERPNext Desk"
						:tooltip="'Open ERPNext Desk'"
						class="jv-deskbtn"
						@click="openDesk"
					/>
					<slot name="right-header" />
				</div>
			</header>
		</slot>
	</Teleport>
</template>

<script setup>
// CRM's LayoutHeader (R2 §1): pages teleport their header content into the
// shell's #app-header strip. `showHeader` flips in nextTick so the Teleport
// target exists before we render into it.
import { ref, nextTick, onMounted } from "vue";
import { Button } from "frappe-ui";

// Persistent "Go to Desk" (same behavior as ChatView's openErpDesk).
function openDesk() {
	window.open("/app", "_blank");
}

const showHeader = ref(false);
onMounted(() => {
	nextTick(() => {
		showHeader.value = true;
	});
});
</script>
