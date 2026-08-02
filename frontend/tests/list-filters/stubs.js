// Shared frappe-ui doubles for the list-filter specs.
//
// frappe-ui's ESM entry does not resolve under vitest (the same reason every
// other .spec.js in this repo module-mocks it), so the panel's specs stub the
// handful of components it mounts. The stubs are deliberately REAL about the
// contract each one has with the panel — Popover emits `open`, FormControl
// emits `update:modelValue` from a native control, Autocomplete emits both
// `update:query` and `update:modelValue` — because those are the wires the
// tests are actually asserting on.
export function frappeUiStubs() {
	return {
		Popover: {
			name: "Popover",
			emits: ["open"],
			template: `<div class="popover">
				<slot name="target" :togglePopover="() => $emit('open')" />
				<slot name="body" />
			</div>`,
		},
		Button: {
			name: "Button",
			inheritAttrs: false,
			props: ["label", "icon", "iconLeft", "variant", "tooltip", "loading"],
			emits: ["click"],
			template: `<button v-bind="$attrs" :data-icon="icon" @click="$emit('click', $event)">
				{{ label }}<slot /><slot name="suffix" />
			</button>`,
		},
		FormControl: {
			name: "FormControl",
			inheritAttrs: false,
			props: ["type", "options", "modelValue", "placeholder", "debounce"],
			emits: ["update:modelValue"],
			template: `<select
					v-if="type === 'select'"
					v-bind="$attrs"
					:value="modelValue"
					@change="$emit('update:modelValue', $event.target.value)"
				>
					<option v-for="o in options || []" :key="String(o.value)" :value="o.value">{{ o.label }}</option>
				</select>
				<input
					v-else
					v-bind="$attrs"
					:type="type"
					:value="modelValue"
					:placeholder="placeholder"
					@input="$emit('update:modelValue', $event.target.value)"
				/>`,
		},
		Autocomplete: {
			name: "Autocomplete",
			inheritAttrs: false,
			props: {
				options: { type: Array, default: () => [] },
				modelValue: { type: [Object, Array, String], default: null },
				// Boolean-typed, like frappe-ui's own prop: a bare `multiple`
				// attribute must cast to true, not to "".
				multiple: { type: Boolean, default: false },
				placeholder: { type: String, default: "" },
				loading: { type: Boolean, default: false },
				bodyClasses: { type: [String, Array], default: "" },
			},
			emits: ["update:modelValue", "update:query"],
			template: `<div class="autocomplete" v-bind="$attrs">
				<slot name="target" :togglePopover="() => {}" />
				<span class="ac-display">{{ display }}</span>
			</div>`,
			computed: {
				display() {
					const v = this.modelValue;
					if (Array.isArray(v)) return v.map((o) => (o && o.label) || o).join(", ");
					return (v && v.label) || "";
				},
			},
		},
		DatePicker: {
			name: "DatePicker",
			inheritAttrs: false,
			props: ["modelValue", "placeholder"],
			emits: ["update:modelValue"],
			template: `<input class="date-picker" v-bind="$attrs" :value="modelValue" :placeholder="placeholder" @input="$emit('update:modelValue', $event.target.value)" />`,
		},
		ErrorMessage: {
			name: "ErrorMessage",
			props: ["message"],
			template: `<div class="error-message" role="alert">{{ message }}</div>`,
		},
		LoadingIndicator: { name: "LoadingIndicator", template: `<span class="loading" />` },
		FeatherIcon: { name: "FeatherIcon", props: ["name"], template: `<i :data-name="name" />` },
		Dropdown: {
			name: "Dropdown",
			props: ["options"],
			template: `<div class="dropdown"><slot /></div>`,
		},
		Dialog: {
			name: "Dialog",
			props: ["options", "modelValue"],
			template: `<div class="dialog"><slot name="body-content" /></div>`,
		},
		Breadcrumbs: { name: "Breadcrumbs", props: ["items"], template: `<nav />` },
		Switch: {
			name: "Switch",
			props: ["modelValue", "label"],
			emits: ["update:modelValue"],
			template: `<button class="switch" @click="$emit('update:modelValue', !modelValue)" />`,
		},
		Avatar: { name: "Avatar", props: ["label", "size"], template: `<span class="avatar" />` },
		Badge: {
			name: "Badge",
			props: ["variant", "theme", "label"],
			template: `<span class="badge"><slot>{{ label }}</slot><slot name="suffix" /></span>`,
		},
	};
}
