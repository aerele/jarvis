<template>
	<button
		class="jv-focus-ring flex h-7.5 cursor-pointer items-center rounded text-ink-gray-8 duration-300 ease-in-out"
		:class="isActive ? 'bg-surface-selected shadow-sm' : 'hover:bg-surface-gray-2'"
		@click="handleClick"
	>
		<div
			class="flex w-full items-center justify-between duration-300 ease-in-out"
			:class="isCollapsed ? 'ml-[3px] p-1' : 'px-2 py-[7px]'"
		>
			<div class="flex items-center truncate">
				<Tooltip :text="label" placement="right" :disabled="!isCollapsed">
					<slot name="icon">
						<FeatherIcon
							v-if="typeof icon === 'string'"
							:name="icon"
							class="flex size-4 items-center text-ink-gray-8"
						/>
						<component
							:is="icon"
							v-else-if="icon"
							class="flex size-4 items-center text-ink-gray-8"
						/>
					</slot>
				</Tooltip>
				<Tooltip :text="label" placement="right" :disabled="isCollapsed" :hoverDelay="1.5">
					<span
						class="flex-1 flex-shrink-0 truncate text-sm duration-300 ease-in-out"
						:class="
							isCollapsed
								? 'ml-0 w-0 overflow-hidden opacity-0'
								: 'ml-2 w-auto opacity-100'
						"
					>
						{{ label }}
					</span>
				</Tooltip>
			</div>
			<slot name="right" />
		</div>
	</button>
</template>

<script setup>
// CRM's SidebarLink template (R2 §2c) with Helpdesk's prop-driven active
// state (D4): active = elevated chip + shadow, never an accent color.
import { FeatherIcon, Tooltip } from "frappe-ui";
import { useRouter } from "vue-router";

const props = defineProps({
	icon: { type: [Object, String, Function], default: null },
	label: { type: String, default: "" },
	to: { type: [Object, String], default: null },
	isCollapsed: { type: Boolean, default: false },
	isActive: { type: Boolean, default: false },
	onClick: { type: Function, default: null },
});

const router = useRouter();

function handleClick() {
	if (props.onClick) return props.onClick();
	if (props.to) router.push(props.to);
}
</script>
