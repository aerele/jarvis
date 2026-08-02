// Shared frappe-ui doubles for the list-filter specs.
//
// frappe-ui's ESM entry does not resolve under vitest (the same reason every
// other .spec.js in this repo module-mocks it), so the panel's specs stub the
// handful of components it mounts. The stubs are deliberately REAL about the
// contract each one has with the panel — FormControl emits `update:modelValue`
// from a native control, Autocomplete emits both `update:query` and
// `update:modelValue` — because those are the wires the tests are actually
// asserting on.
export function frappeUiStubs() {
	return {
		// Popover's open/close contract is copied from frappe-ui 0.1.278's
		// Popover.vue, INCLUDING its sharp edge: the `#target` slot's
		// `togglePopover` goes through frappe-ui's own `isOpen` setter, which
		// emits `update:show` and NOTHING ELSE. `open`/`close` come only from
		// `onUpdateOpen`, i.e. from reka-ui's PopoverRoot, and because frappe-ui
		// binds `v-model:open` with a real boolean that root is fully controlled
		// and only reports changes IT initiated (outside click, Escape). So a
		// panel opened from its own trigger button never sees `open`.
		//
		// A stub that emitted `open` from `togglePopover` would be a fiction, and
		// it hid exactly this defect once already: the field picker never asked
		// for its schema in a browser while the spec that "covered" it passed.
		// tests/list-filters/FilterGroupPopover.spec.js pins this shape against
		// the real component so it cannot drift back.
		Popover: {
			name: "Popover",
			props: { show: { type: Boolean, default: undefined } },
			emits: ["open", "close", "update:show"],
			data: () => ({ internalOpen: false }),
			computed: {
				isOpen: {
					get() {
						return this.show !== undefined ? this.show : this.internalOpen;
					},
					set(value) {
						if (this.show === undefined) this.internalOpen = value;
						this.$emit("update:show", value);
					},
				},
			},
			methods: {
				// the `#target` slot's handle — frappe-ui's own setter path
				togglePopover(flag) {
					this.isOpen = flag == null ? !this.isOpen : Boolean(flag);
				},
				// what reka-ui's PopoverRoot reports for a change IT made; frappe-ui
				// relays it as update:show AND open/close, on top of the update:show
				// the v-model setter already emitted.
				rekaUpdateOpen(value) {
					this.isOpen = value;
					this.$emit("update:show", value);
					this.$emit(value ? "open" : "close");
				},
			},
			// The body renders unconditionally: these specs assert on panel content
			// without driving open state, and the real body is portalled out of the
			// component's own tree anyway.
			template: `<div class="popover">
				<slot name="target" :togglePopover="togglePopover" :isOpen="isOpen" />
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
		Badge: {
			name: "Badge",
			props: ["variant", "theme", "label"],
			template: `<span class="badge"><slot>{{ label }}</slot><slot name="suffix" /></span>`,
		},
	};
}
