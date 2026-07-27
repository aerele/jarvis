<script setup>
import { ref } from "vue";
import BrandMark from "../components/BrandMark.vue";
import { agentName } from "@/branding";
import { call } from "frappe-ui";

// The app's own sign-in, inside the PWA's scope.
//
// Without it, a logged-out visit redirected to the Desk's /login — outside the
// manifest scope, which an installed PWA hands off to the browser. The user
// taps the Jarvis icon and lands in a Chrome tab. Signing in here keeps them in
// the app, which is the whole point of installing it.

// Any Frappe user id, not just an email: Administrator has no email address,
// and a username-based account never will. The field is plain text so the
// browser's email format check can't reject a valid id before submit runs.
const userId = ref("");
const password = ref("");
const showPassword = ref(false);
const busy = ref(false);
const error = ref("");

async function submit() {
	if (busy.value || !userId.value.trim() || !password.value) return;
	busy.value = true;
	error.value = "";
	try {
		const r = await call("login", { usr: userId.value.trim(), pwd: password.value });

		// Two-factor is a multi-step flow (tmp_id + OTP) this screen doesn't
		// implement. Say so plainly instead of leaving the user on a form that
		// will never succeed.
		if (r?.verification || r?.tmp_id) {
			error.value = "This account uses two-factor sign-in. Please sign in on the web first.";
			busy.value = false;
			return;
		}

		// A full navigation, not a router push: the CSRF token is bound to the
		// session, and logging in mints a NEW session. The token this page was
		// rendered with died with the guest session, so every write after an
		// in-page transition would fail CSRF. Reloading re-renders the shell with
		// the new session's token — and it stays inside /jarvis-mobile, so an
		// installed app never leaves itself.
		window.location.href = "/jarvis-mobile";
	} catch (e) {
		// Frappe throws AuthenticationError (401) for bad credentials; anything
		// else is worth showing verbatim rather than flattening to "login failed".
		const msg = String(e?.message || e || "");
		error.value = /auth|password|incorrect|invalid login/i.test(msg)
			? "Those sign-in details aren't right."
			: msg || "Couldn't sign in. Try again.";
		busy.value = false;
	}
}
</script>

<template>
	<div class="jv-login jv-safe-bottom">
		<div class="jv-login-head">
			<BrandMark :size="56" />
			<h1>{{ agentName }}</h1>
			<p>Your AI teammate. Sign in to pick up where you left off.</p>
		</div>

		<form class="jv-login-form" @submit.prevent="submit">
			<label>
				<span>Email</span>
				<input
					v-model="userId"
					type="text"
					name="username"
					autocomplete="username"
					autocapitalize="none"
					autocorrect="off"
					placeholder="jarvis@mail.com"
					required
				/>
			</label>

			<label>
				<span>Password</span>
				<div class="jv-pw">
					<input
						v-model="password"
						:type="showPassword ? 'text' : 'password'"
						name="password"
						autocomplete="current-password"
						placeholder="••••••••"
						required
					/>
					<button
						type="button"
						class="jv-pw-toggle"
						:aria-label="showPassword ? 'Hide password' : 'Show password'"
						:aria-pressed="showPassword"
						@click="showPassword = !showPassword"
					>
						<svg
							v-if="showPassword"
							viewBox="0 0 24 24"
							width="20"
							height="20"
							fill="none"
							stroke="currentColor"
							stroke-width="1.8"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path
								d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"
							/>
							<path d="M9.88 9.88a3 3 0 1 0 4.24 4.24M1 1l22 22" />
						</svg>
						<svg
							v-else
							viewBox="0 0 24 24"
							width="20"
							height="20"
							fill="none"
							stroke="currentColor"
							stroke-width="1.8"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
							<circle cx="12" cy="12" r="3" />
						</svg>
					</button>
				</div>
			</label>

			<div v-if="error" class="jv-login-error">{{ error }}</div>

			<button
				class="jv-login-btn"
				type="submit"
				:disabled="busy || !userId.trim() || !password"
			>
				<span v-if="busy" class="jv-spinner" />
				<span v-else>Sign in</span>
			</button>
		</form>

		<!-- Password reset is a Desk flow and lives outside the PWA scope, so it is
		     a plain link: an installed app opens it in the browser deliberately,
		     rather than appearing to break. -->
		<a class="jv-login-alt" href="/login#forgot">Forgot your password?</a>
	</div>
</template>

<style scoped>
.jv-login {
	flex: 1;
	min-height: 0;
	overflow-y: auto;
	display: flex;
	flex-direction: column;
	justify-content: center;
	gap: 22px;
	padding: 32px 24px;
}
.jv-login-head {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 10px;
	text-align: center;
}
.jv-login-head h1 {
	margin: 4px 0 0;
	font-size: 24px;
	font-weight: 600;
	letter-spacing: -0.3px;
	color: var(--ink9);
}
.jv-login-head p {
	margin: 0;
	max-width: 280px;
	font-size: 14px;
	line-height: 1.5;
	color: var(--ink5);
}
.jv-login-form {
	display: flex;
	flex-direction: column;
	gap: 14px;
}
.jv-login-form label {
	display: flex;
	flex-direction: column;
	gap: 6px;
}
.jv-login-form span {
	font-size: 13px;
	font-weight: 500;
	color: var(--ink7);
}
.jv-login-form input {
	height: 48px;
	padding: 0 14px;
	border: 1px solid var(--border2);
	border-radius: 12px;
	background: var(--card);
	color: var(--ink9);
	font: inherit;
	font-size: 15px;
	outline: none;
}
.jv-login-form input:focus {
	border-color: var(--accent);
}
/* Password field with an in-field show/hide toggle. The input keeps its own
   border/background (via .jv-login-form input); the eye button floats over its
   right edge, and the input reserves room so the dots never slide under it. */
.jv-pw {
	position: relative;
}
.jv-pw input {
	width: 100%;
	padding-right: 46px;
}
.jv-pw-toggle {
	position: absolute;
	top: 0;
	right: 0;
	display: grid;
	place-items: center;
	width: 46px;
	height: 48px;
	padding: 0;
	border: 0;
	background: transparent;
	color: var(--ink5);
	cursor: pointer;
}
.jv-pw-toggle:active {
	color: var(--ink8);
}
.jv-login-error {
	padding: 11px 12px;
	border-radius: 10px;
	background: var(--red-bg);
	color: var(--red);
	font-size: 13px;
	line-height: 1.4;
}
.jv-login-btn {
	display: grid;
	place-items: center;
	height: 50px;
	margin-top: 2px;
	border: 0;
	border-radius: 12px;
	background: var(--accent-solid);
	color: #fff;
	font: inherit;
	font-size: 15px;
	font-weight: 600;
	cursor: pointer;
}
.jv-login-btn:disabled {
	opacity: 0.55;
	cursor: default;
}
.jv-login-alt {
	align-self: center;
	font-size: 13px;
	color: var(--ink5);
	text-decoration: none;
}
.jv-spinner {
	width: 18px;
	height: 18px;
	border-radius: 50%;
	border: 2px solid rgba(255, 255, 255, 0.35);
	border-top-color: #fff;
	animation: jv-spin 0.7s linear infinite;
}
@keyframes jv-spin {
	to {
		transform: rotate(360deg);
	}
}
</style>
