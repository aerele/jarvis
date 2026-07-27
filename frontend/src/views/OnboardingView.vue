<template>
	<!-- Root: jv-ob-root + the jv-dark class + paletteVars stay bound here even
		 though this view is otherwise migrated to frappe-ui/Tailwind. Two
		 independent, load-bearing reasons, neither of them decoration:

		 1. SetupNeuralNet's readColors() (onboarding/SetupNeuralNet.vue) reads
			--text-3/--surface/--surface-3 via getComputedStyle on ITS OWN root,
			which only resolve because they inherit down from paletteVars applied
			somewhere above it in the DOM - there is no :root fallback (design.md
			§2.2). Relocating/dropping this binding would silently render the
			connect-step canvas with unresolved colors.
		 2. JvCombo.vue (Details step) and LlmPoolEditor.vue (Connect step) - both
			out of scope for this migration - read the SAME jv-* var(--...) custom
			properties in their own scoped CSS (12 and 182 usages respectively) and
			render raw <input> elements that frontend/src/index.css targets by the
			literal ".jv-ob-root" selector to suppress the @tailwindcss/forms blue
			focus ring (".jv-ob-root :where(input...):focus"). Renaming this class
			would silently reintroduce that blue ring on every input inside
			LlmPoolEditor/JvCombo, on this page only. -->
	<div class="jv-ob-root" :class="{ 'jv-dark': dark }" :style="paletteVars">
		<main class="relative z-10 min-w-0 flex-1 overflow-y-auto">
			<!-- Fills the viewport so the card centers vertically when short; a step
				 taller than the viewport grows and the card top-aligns. -->
			<div class="box-border flex min-h-full items-center justify-center px-5 pb-15 pt-6.5">
				<div class="mx-auto flex w-full max-w-[1080px] flex-col items-center">
					<!-- brand header: JarvisMark + name + per-step subtitle -->
					<div class="mb-2 flex items-center justify-center gap-2.5">
						<JarvisMark :size="30" :radius="8" />
						<span class="text-base font-semibold">{{ agentName }}</span>
						<span
							class="border-l border-outline-gray-1 pl-2.5 text-p-sm text-ink-gray-6"
							>{{ frameSub }}</span
						>
					</div>

					<!-- step rail: flat progress segments with labels (design.md §4.3 —
					 no numbered circles, no connector lines). Hidden on the intro
					 tour (chromeless) and on the single-step self-host track. -->
					<div
						v-if="railIndex >= 0"
						class="my-4 flex w-full max-w-[720px] items-stretch gap-2"
						role="list"
						aria-label="Setup steps"
					>
						<div
							v-for="(s, i) in RAIL"
							:key="s.id"
							role="listitem"
							class="flex flex-1 flex-col gap-1.5"
							:aria-current="i === railIndex ? 'step' : undefined"
						>
							<span
								class="text-p-sm"
								:class="
									i === railIndex
										? 'font-medium text-ink-gray-9'
										: i < railIndex
										? 'text-ink-gray-7'
										: 'text-ink-gray-5'
								"
								>{{ s.label }}</span
							>
							<span
								class="h-1 rounded-full"
								:class="i <= railIndex ? 'bg-surface-gray-7' : 'bg-surface-gray-3'"
							></span>
						</div>
					</div>

					<div
						class="w-full overflow-hidden rounded-2xl border border-outline-gray-1 bg-surface-white shadow-2xl"
					>
						<!-- ===== Intro tour (fresh starts only; reconcile routes mid-flight
							 signups straight to the right step, past the tour) ===== -->
						<TourIntro
							v-if="state.step === 'intro'"
							@finish="startWizard"
							@skip="startWizard"
						/>

						<!-- ===== Choose Your Plan ===== -->
						<section v-else-if="state.step === 'plan'" class="ob-screen">
							<div class="ob-body">
								<div class="ob-head">
									<h1>Choose your plan</h1>
									<p>
										Start free. Upgrade or extend anytime, with no
										auto-renewal.
									</p>
								</div>
								<div
									v-if="state.plansLoading"
									class="mb-5 text-center text-p-sm text-ink-gray-5"
								>
									Loading plans…
								</div>
								<Banner
									v-else-if="state.plansErr"
									type="error"
									:message="state.plansErr"
								>
									<template #action>
										<Button label="Retry" @click="loadPlansSafe" />
									</template>
								</Banner>
								<div
									v-else-if="!state.plans.length"
									class="mb-5 text-center text-p-sm text-ink-gray-5"
								>
									No plans are available right now. Please contact support.
								</div>
								<div
									v-else
									class="grid grid-cols-1 gap-3 sm:grid-cols-3"
									role="radiogroup"
									aria-label="Plan"
								>
									<div
										v-for="p in state.plans"
										:key="p.name"
										class="relative cursor-pointer rounded-lg border p-4.5 transition-colors focus-visible:outline-none focus-visible:ring focus-visible:ring-outline-gray-3"
										:class="
											state.planName === p.name
												? 'border-outline-gray-5 ring-1 ring-outline-gray-5'
												: 'border-outline-gray-1 hover:border-outline-gray-2 hover:bg-surface-gray-1'
										"
										role="radio"
										:aria-checked="state.planName === p.name"
										tabindex="0"
										@click="state.planName = p.name"
										@keydown.enter.prevent="state.planName = p.name"
										@keydown.space.prevent="state.planName = p.name"
									>
										<div
											class="absolute right-4 top-4 grid h-4 w-4 place-items-center rounded-full border transition-colors"
											:class="
												state.planName === p.name
													? 'border-outline-gray-5 bg-surface-gray-7'
													: 'border-outline-gray-3'
											"
										>
											<span
												v-if="state.planName === p.name"
												class="h-1.5 w-1.5 rounded-full bg-surface-white"
											></span>
										</div>
										<div class="text-base font-medium text-ink-gray-9">
											{{ p.plan_name }}
										</div>
										<div
											class="mb-0.5 mt-2.5 text-[22px] font-medium text-ink-gray-9"
										>
											{{ planAmount(p.price_inr)
											}}<span
												v-if="planSuffix(p.price_inr, p.billing_cycle)"
												class="text-p-sm font-normal text-ink-gray-5"
											>
												{{
													planSuffix(p.price_inr, p.billing_cycle)
												}}</span
											>
										</div>
										<div class="text-xs text-ink-gray-5">
											{{ planCycleLabel(p) }}
										</div>
										<ul class="mt-3.5 grid gap-2">
											<li
												v-for="(f, k) in planFeatures(p)"
												:key="k"
												class="flex items-start gap-2 text-p-sm text-ink-gray-7"
											>
												<FeatherIcon
													name="check"
													class="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-green-3"
												/>{{ f }}
											</li>
											<li
												v-if="!planFeatures(p).length"
												class="text-p-sm text-ink-gray-5"
											>
												{{ p.billing_cycle }} plan
											</li>
										</ul>
									</div>
								</div>
							</div>
							<div class="ob-foot">
								<button class="ob-back" @click="goBack">
									<FeatherIcon
										name="chevron-left"
										class="h-3.5 w-3.5 text-ink-gray-5"
									/>Back to tour
								</button>
								<button v-if="canSelfHost" class="ob-link" @click="enterSelfhost">
									Self-hosted? Connect your own openclaw
								</button>
								<Button
									variant="solid"
									label="Continue"
									:disabled="!state.planName"
									@click="onPlanContinue"
								/>
							</div>
						</section>

						<!-- ===== Your Details ===== -->
						<section v-else-if="state.step === 'details'" class="ob-screen">
							<div class="ob-body">
								<div class="ob-head">
									<h1>Your details</h1>
									<p>
										We'll set {{ agentName }} up for this workspace and send
										receipts here.
									</p>
								</div>
								<div
									class="ob-details-form mx-auto grid max-w-[620px] grid-cols-2 gap-3.5 max-[820px]:grid-cols-1"
								>
									<div
										class="col-span-2 -mb-1 mt-2 text-base font-semibold text-ink-gray-9 first:mt-0"
									>
										Account
									</div>
									<FormControl
										id="jv-ob-email"
										type="email"
										variant="outline"
										label="Work email"
										v-model="state.email"
										placeholder="you@company.com"
										autocomplete="email"
										required
										aria-required="true"
										@keydown.enter="onDetailsSubmit"
									/>
									<FormControl
										id="jv-ob-contact"
										type="tel"
										variant="outline"
										label="Contact number (optional)"
										v-model="state.contact"
										placeholder="+91 98765 43210"
										autocomplete="tel"
										@keydown.enter="onDetailsSubmit"
									/>
									<div class="col-span-2 flex flex-col gap-1.5">
										<label for="jv-ob-company" class="text-xs text-ink-gray-5"
											>Company</label
										>
										<JvCombo
											id="jv-ob-company"
											:model-value="state.company"
											@update:model-value="(v) => (state.company = v)"
											allow-custom
											aria-required
											autocomplete="organization"
											:options="state.companies"
											placeholder="Acme Inc."
											@enter="onDetailsSubmit"
										/>
									</div>
									<div
										class="col-span-2 mt-2 text-base font-semibold text-ink-gray-9"
									>
										Billing
									</div>
									<div class="col-span-2 -mt-1 text-p-xs text-ink-gray-5">
										Billing details are kept with your account for upcoming
										invoicing.
									</div>
									<FormControl
										id="jv-ob-addr"
										class="col-span-2"
										type="text"
										variant="outline"
										label="Billing address (optional)"
										v-model="state.billingAddress"
										placeholder="Street, area"
										autocomplete="street-address"
										@keydown.enter="onDetailsSubmit"
									/>
									<FormControl
										id="jv-ob-city"
										type="text"
										variant="outline"
										label="City (optional)"
										v-model="state.city"
										placeholder="Chennai"
										autocomplete="address-level2"
										@keydown.enter="onDetailsSubmit"
									/>
									<FormControl
										id="jv-ob-gstin"
										type="text"
										variant="outline"
										label="GSTIN (optional)"
										v-model="state.gstin"
										placeholder="33ABCDE1234F1Z5"
										@keydown.enter="onDetailsSubmit"
									/>
								</div>
								<Banner
									v-if="state.detailsErr"
									type="error"
									:message="state.detailsErr"
									role="alert"
									aria-live="polite"
									class="mx-auto mt-5 max-w-[620px]"
								/>
							</div>
							<div class="ob-foot">
								<button class="ob-back" @click="goBack">
									<FeatherIcon
										name="chevron-left"
										class="h-3.5 w-3.5 text-ink-gray-5"
									/>Back
								</button>
								<Button
									variant="solid"
									label="Continue"
									@click="onDetailsSubmit"
								/>
							</div>
						</section>

						<!-- ===== Review & Pay (renderPay / renderVerifyEmail / startPay /
							 openCheckout preserved verbatim in behavior) ===== -->
						<section v-else-if="state.step === 'pay'" class="ob-screen">
							<template v-if="state.provisioning || state.provisionErr">
								<div class="ob-body">
									<div class="ob-head">
										<h1>Setting up your workspace</h1>
										<p v-if="state.provisioning">
											{{
												isTrialPlan
													? "Auto-pay authorized — nothing charged until your trial ends."
													: "Payment received."
											}}
											We're provisioning your {{ agentName }} workspace. This
											usually takes under a minute…
										</p>
									</div>
									<div
										v-if="state.provisioning"
										class="mt-2.5 flex justify-center"
										aria-hidden="true"
									>
										<LoadingIndicator class="h-8 w-8 text-ink-gray-6" />
									</div>
									<Banner
										v-if="state.provisionErr"
										type="error"
										:message="state.provisionErr"
										role="alert"
									/>
								</div>
								<div v-if="state.provisionErr" class="ob-foot justify-end">
									<Button
										variant="solid"
										label="Retry"
										@click="proceedAfterPay"
									/>
								</div>
							</template>
							<template v-else-if="state.payPhase === 'verify'">
								<div class="ob-body">
									<div class="ob-head">
										<h1>Check your email</h1>
										<p>
											We sent a confirmation link to
											<b>{{ state.email || "your email" }}</b
											>. Click the link to verify your address, then come
											back here and click the button below to continue to
											payment.
										</p>
									</div>
									<p class="text-center text-p-sm text-ink-gray-5">
										The link expires in 24 hours. Check your spam folder if it
										doesn't arrive.
									</p>
									<Banner
										v-if="state.payErr"
										type="error"
										:message="state.payErr"
									/>
								</div>
								<div class="ob-foot justify-end">
									<Button
										variant="solid"
										:loading="state.payBusy"
										loading-text="Working…"
										label="I've verified my email"
										@click="onVerifyCheck"
									/>
								</div>
							</template>
							<template v-else-if="state.successData">
								<div class="ob-body">
									<div class="ob-head">
										<h1>
											{{
												isTrialPlan
													? "Free trial started"
													: "Payment complete"
											}}
										</h1>
										<p>You're all set. Continue to connect your AI.</p>
									</div>
								</div>
								<div class="ob-foot justify-end">
									<Button variant="solid" label="Continue" @click="goNext" />
								</div>
							</template>
							<template v-else>
								<div class="ob-body">
									<div class="ob-head">
										<h1>Review &amp; pay</h1>
										<p>
											{{
												isTrialPlan
													? "Confirm the details below. You'll authorize auto-pay securely via Razorpay — nothing is charged until your trial ends."
													: "Confirm the details below. You'll complete payment securely via Razorpay."
											}}
										</p>
									</div>
									<div
										class="mx-auto max-w-[560px] overflow-hidden rounded-lg border border-outline-gray-1"
									>
										<div
											class="flex items-center justify-between gap-3 border-b border-outline-gray-1 px-4 py-3 text-p-sm"
										>
											<span class="text-ink-gray-5">Plan</span
											><b class="font-medium text-ink-gray-9">{{
												planRowLabel
											}}</b>
										</div>
										<div
											class="flex items-center justify-between gap-3 border-b border-outline-gray-1 px-4 py-3 text-p-sm"
										>
											<span class="text-ink-gray-5">Company</span
											><b class="font-medium text-ink-gray-9">{{
												state.company
											}}</b>
										</div>
										<div
											class="flex items-center justify-between gap-3 border-b border-outline-gray-1 px-4 py-3 text-p-sm"
										>
											<span class="text-ink-gray-5">Billed to</span
											><b class="font-medium text-ink-gray-9">{{
												state.email
											}}</b>
										</div>
										<div
											class="flex items-center justify-between gap-3 bg-surface-gray-1 px-4 py-3 text-p-sm"
										>
											<span class="text-ink-gray-5">Due today</span
											><b class="text-base font-semibold text-ink-gray-9">{{
												dueTodayLabel
											}}</b>
										</div>
									</div>
									<div
										class="mx-auto mt-3.5 flex max-w-[560px] items-center justify-center gap-1.5 text-center text-xs text-ink-gray-5"
									>
										<FeatherIcon name="lock" class="h-3.5 w-3.5" />
										Secured by
										{{
											state.paymentProvider === "cashfree"
												? "Cashfree"
												: "Razorpay"
										}}
									</div>
									<div
										v-if="showProviderChooser"
										class="ob-provseg mx-auto mt-3.5 flex max-w-[360px] gap-1 rounded-xl border border-outline-gray-1 bg-surface-gray-2 p-1"
										:class="{
											'max-w-[220px] cursor-default': isSingleProvider,
										}"
										:role="isSingleProvider ? undefined : 'radiogroup'"
										:aria-label="
											isSingleProvider ? undefined : 'Payment method'
										"
									>
										<button
											v-if="providerAvailable('razorpay')"
											type="button"
											class="ob-provseg-opt flex min-h-11 flex-1 items-center justify-center rounded-lg px-2.5 py-2 transition-[background,box-shadow,opacity] focus-visible:ring focus-visible:ring-outline-gray-3"
											:class="
												state.paymentProvider === 'razorpay' ||
												isSingleProvider
													? 'bg-surface-white opacity-100 shadow-sm'
													: 'cursor-pointer opacity-60 hover:opacity-85'
											"
											:role="isSingleProvider ? undefined : 'radio'"
											:aria-checked="
												isSingleProvider
													? undefined
													: state.paymentProvider === 'razorpay'
											"
											:aria-label="
												isSingleProvider
													? 'Payment method: Razorpay'
													: 'Razorpay'
											"
											:disabled="isSingleProvider"
											@click="chooseProvider('razorpay')"
										>
											<span
												class="inline-flex items-center gap-1.5"
												aria-hidden="true"
											>
												<svg viewBox="0 0 20 24" width="15" height="18">
													<path
														fill="#3395ff"
														d="M14.4 0 8 12.1l1.6 3.8L18 3.5z"
													/>
													<path
														fill="#0b2a6b"
														d="M9.2 8 2 24h4.7l3-7.5 2.1-4.6z"
													/>
												</svg>
												<span
													class="text-base font-semibold tracking-tight"
													style="color: #0b2a6b"
													>Razorpay</span
												>
											</span>
										</button>
										<button
											v-if="providerAvailable('cashfree')"
											type="button"
											class="ob-provseg-opt flex min-h-11 flex-1 items-center justify-center rounded-lg px-2.5 py-2 transition-[background,box-shadow,opacity] focus-visible:ring focus-visible:ring-outline-gray-3"
											:class="
												state.paymentProvider === 'cashfree' ||
												isSingleProvider
													? 'bg-surface-white opacity-100 shadow-sm'
													: 'cursor-pointer opacity-60 hover:opacity-85'
											"
											:role="isSingleProvider ? undefined : 'radio'"
											:aria-checked="
												isSingleProvider
													? undefined
													: state.paymentProvider === 'cashfree'
											"
											:aria-label="
												isSingleProvider
													? 'Payment method: Cashfree'
													: 'Cashfree'
											"
											:disabled="isSingleProvider"
											@click="chooseProvider('cashfree')"
										>
											<img
												:src="cashfreeLogo"
												alt="Cashfree"
												class="block h-5.5 w-auto"
											/>
										</button>
									</div>
									<Banner
										v-if="state.payErr"
										type="error"
										:message="state.payErr"
										class="mx-auto mt-3.5 max-w-[560px]"
									/>
								</div>
								<div class="ob-foot">
									<button
										class="ob-back"
										:disabled="state.payBusy"
										@click="goBack"
									>
										<FeatherIcon
											name="chevron-left"
											class="h-3.5 w-3.5 text-ink-gray-5"
										/>Back
									</button>
									<Button
										variant="solid"
										:disabled="state.payBusy"
										:loading="state.payBusy"
										loading-text="Working…"
										:label="payCta"
										@click="onPayClick"
									/>
								</div>
							</template>
						</section>

						<!-- ===== Connect Your AI (managed) - embeds the shared LlmPoolEditor
							 (same one AccountView uses), :modes="['quick']". The component owns
							 save_llm_pool; this step is the post-save readiness handoff.
							 v-show (not v-if) so the editor stays MOUNTED while its own save()
							 is still awaiting. ===== -->
						<section v-else-if="state.step === 'connect'" class="ob-screen">
							<div class="ob-body">
								<div v-show="state.finishing">
									<div class="ob-head">
										<h1>Setting up {{ agentName }}</h1>
										<p>
											{{
												state.finishSubtitle ||
												"Bringing your workspace online, taking you to chat…"
											}}
										</p>
									</div>
									<!-- min-height (not h-full) is load-bearing: SetupNeuralNet's
										 canvas fills via absolute+inset-0, and percentage heights
										 don't resolve against a min-height parent - see its own
										 comment. Don't change this to a fixed h-*. -->
									<div class="relative mt-2 min-h-[380px] flex-1">
										<SetupNeuralNet :dark="dark" />
									</div>
								</div>
								<div v-show="!state.finishing">
									<div class="ob-head">
										<h1>Give {{ agentName }} a brain</h1>
										<p>
											Pick which AI powers {{ agentName }}. You can change
											this anytime in Settings → AI models.
										</p>
									</div>
									<div class="mx-auto max-w-[640px]">
										<LlmPoolEditor
											ref="poolRef"
											:editable="true"
											:modes="['quick']"
											:footerless="true"
											@saved="onConnected"
											@ready="connectReady = $event"
										/>
									</div>
									<Banner
										v-if="state.finishNote"
										type="info"
										:message="state.finishNote"
									>
										<template #action>
											<Button
												variant="solid"
												:label="`Continue to ${agentName}`"
												@click="forceContinue"
											/>
										</template>
									</Banner>
								</div>
							</div>
							<div v-if="!state.finishing" class="ob-foot">
								<!-- No Back on a reconciled resume: signup/payment already completed
									 in a previous session, so there is no local pay/review context to
									 go back to (re-running startSignup there would double-sign-up). -->
								<button
									v-if="!state.reconciledConnect"
									class="ob-back"
									:disabled="savingConnect"
									@click="goBack"
								>
									<FeatherIcon
										name="chevron-left"
										class="h-3.5 w-3.5 text-ink-gray-5"
									/>Back
								</button>
								<span v-else></span>
								<!-- Always rendered; disabled until the editor reports a savable config,
									 so the step never shows without a primary action. -->
								<button
									class="jv-ob-btn jv-ob-btn-grad"
									:disabled="!connectReady || savingConnect"
									@click="saveConnect"
								>
									{{ savingConnect ? "Connecting…" : "Start chatting" }}
								</button>
							</div>
						</section>

						<!-- ===== Self-host (reached via the quiet Plan-step link; logic
							 unchanged, field names/args match test_connection /
							 save_self_hosted verbatim) ===== -->
						<section v-else-if="state.step === 'selfhost'" class="ob-screen">
							<div class="ob-body">
								<div class="ob-head">
									<h1>Connect your openclaw</h1>
									<p>
										Point {{ agentName }} at <b>your own</b> openclaw server.
										{{ agentName }}
										connects over HTTP with a bearer token. No Aerele
										persona/skills. Validate first, then connect.
									</p>
								</div>
								<div class="mx-auto flex max-w-[620px] flex-col gap-3.5">
									<FormControl
										id="jv-ob-sh-url"
										type="text"
										variant="outline"
										label="openclaw URL"
										v-model="state.shUrl"
										placeholder="http://host.docker.internal:19060"
									/>
									<FormControl
										id="jv-ob-sh-token"
										type="password"
										variant="outline"
										label="Gateway token"
										v-model="state.shToken"
										placeholder="paste your openclaw gateway token"
										autocomplete="off"
									/>
									<FormControl
										type="checkbox"
										v-model="state.shStream"
										label="Stream responses token-by-token (recommended)"
									/>
									<FormControl
										type="checkbox"
										v-model="state.shDeep"
										label="Run deep chat test (slower, sends one message)"
									/>
									<div>
										<Button
											label="Test connection"
											:disabled="state.shTestBusy"
											:loading="state.shTestBusy"
											loading-text="Testing…"
											@click="runSelfHostTest"
										/>
									</div>
									<div v-if="state.shTestBusy" class="ob-note">Testing…</div>
									<div
										v-else-if="state.shTestResult"
										class="mb-1 mt-3.5 text-p-sm leading-relaxed"
									>
										<div
											class="mb-1 font-medium"
											:class="
												state.shTestResult.ok
													? 'text-ink-green-3'
													: 'text-ink-red-3'
											"
										>
											{{
												state.shTestResult.ok
													? "All required checks passed."
													: "Some checks failed. Fix them and retry."
											}}
										</div>
										<div
											v-for="(c, i) in state.shTestResult.checks || []"
											:key="i"
											class="flex items-start gap-1.5 py-0.5"
											:class="
												c.advisory ? 'text-ink-gray-5' : 'text-ink-gray-8'
											"
										>
											<FeatherIcon
												v-if="c.ok"
												name="check-circle"
												class="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-green-3"
											/>
											<FeatherIcon
												v-else-if="c.advisory"
												name="alert-triangle"
												class="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-amber-3"
											/>
											<FeatherIcon
												v-else
												name="x-circle"
												class="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-red-3"
											/>
											<span
												><b>{{ c.check }}</b> · {{ c.detail || ""
												}}<span
													v-if="c.advisory"
													class="italic text-ink-gray-5"
												>
													· advisory</span
												></span
											>
										</div>
									</div>
									<Banner
										v-if="state.shWarning"
										type="warning"
										:message="state.shWarning"
									/>
									<Banner
										v-if="state.shErr"
										type="error"
										:message="state.shErr"
										role="alert"
										aria-live="polite"
									/>
									<div v-if="state.finishing" class="ob-note">
										Finishing setup…
									</div>
									<Banner
										v-else-if="state.finishNote"
										type="info"
										:message="state.finishNote"
									>
										<template #action>
											<Button
												variant="solid"
												:label="`Continue to ${agentName}`"
												@click="forceContinue"
											/>
										</template>
									</Banner>
								</div>
							</div>
							<div class="ob-foot">
								<!-- Stay disabled through the post-save readiness poll (finishing) too;
									 both flags drop on the failure paths so retry stays possible. -->
								<button
									class="ob-back"
									:disabled="state.shSaveBusy || state.finishing"
									@click="backFromSelfhost"
								>
									<FeatherIcon
										name="chevron-left"
										class="h-3.5 w-3.5 text-ink-gray-5"
									/>Back
								</button>
								<Button
									variant="solid"
									:disabled="state.shSaveBusy || state.finishing"
									:loading="state.shSaveBusy || state.finishing"
									loading-text="Connecting…"
									label="Connect"
									@click="onSelfHostSave"
								/>
							</div>
						</section>
					</div>
				</div>
			</div>
		</main>
	</div>
</template>

<script setup>
import { reactive, ref, computed, onMounted, watch } from "vue";
import { Button, FormControl, FeatherIcon, LoadingIndicator } from "frappe-ui";
import { useJarvisTheme } from "@/theme";
import LlmPoolEditor from "@/components/LlmPoolEditor.vue";
import JvCombo from "@/components/JvCombo.vue";
import JarvisMark from "@/components/JarvisMark.vue";
import Banner from "@/components/Banner.vue";
import TourIntro from "@/onboarding/TourIntro.vue";
import SetupNeuralNet from "@/onboarding/SetupNeuralNet.vue";
import cashfreeLogo from "@/assets/cashfree.png";
import {
	STEPS_MANAGED,
	STEPS_SELFHOST,
	nextStep,
	prevStep,
	verifyPollAction,
	notReadyNote,
	syncStatusNote,
} from "@/onboarding/steps";
import { inr, planAmount, planSuffix } from "@/account/format";
import {
	checkSignupPaymentState,
	isReadyForChat,
	getLlmSyncStatus,
	listPlans,
	listPaymentProviders,
	startSignup,
	finishPayment,
	saveSelfHosted,
	testSelfHostConnection,
	getAccountDefaults,
	syncConnection,
} from "@/api";
import { errMessage as errMsg } from "@/lib/errors";
import { agentName } from "@/branding";

const { effectiveDark: dark, paletteVars } = useJarvisTheme();

// Self-host connect (save_self_hosted / test_connection) stays System-Manager-
// ONLY (owner trust-boundary decision). Managed onboarding is widened to the
// Jarvis Admin tier, but the self-host side-branch entry point + its auto-
// reconcile must be hidden/short-circuited for a Jarvis-Admin-not-SM so they
// never land on a step whose save 403s. NOT `|| window.is_jarvis_admin`.
const canSelfHost = !!window.is_system_manager;

// The 4 named wizard steps shown on the rail. The intro tour and the
// self-host track are chromeless (no rail entry).
const RAIL = [
	{ id: "plan", label: "Plan" },
	{ id: "details", label: "Details" },
	{ id: "pay", label: "Pay" },
	{ id: "connect", label: "Connect" },
];

// Frame subtitle next to the brand mark, mirroring the active step's title.
const FRAME_SUBS = {
	intro: "Meet your ERPNext assistant",
	plan: "Choose your plan",
	details: "Your details",
	pay: "Review & pay",
	connect: `Give ${agentName} a brain`,
	selfhost: "Self-hosted setup",
};

// ---- step machine -----------------------------------------------------------
// `state.step` walks STEPS_MANAGED (intro → plan → details → pay → connect);
// the self-host track is a side branch entered from the Plan step's quiet link
// (enterSelfhost/backFromSelfhost below) and via reconcile.
const state = reactive({
	mode: "managed",
	step: "intro",
	// details (Your Details step)
	email: "",
	company: "",
	companies: [],
	detailsErr: "",
	// Collected by the redesign but NOT submitted yet:
	// jarvis.onboarding.start_signup(email, company, plan) and
	// admin_client.signup(email, company_name, plan, coupon=None) accept no
	// contact/billing kwargs, and the admin-side signup contract is external
	// to this repo. Threading them through would break the API contract.
	// TODO(backend): pass contact + billingAddress/city/gstin through
	// start_signup → admin signup once those endpoints accept them.
	contact: "",
	billingAddress: "",
	city: "",
	gstin: "",
	// plan (Choose Your Plan step)
	plans: [],
	planName: null,
	plansLoading: false,
	plansErr: "",
	// pay (renderPay / renderVerifyEmail / startPay / openCheckout)
	payPhase: "review", // "review" | "verify" - mirrors desk's step-3 vs "check your email" sub-screen
	paymentProvider: "razorpay", // gateway chosen on Review & Pay: "razorpay" | "cashfree"
	// Gateways the operator has actually enabled, narrowed to what this build
	// can render. Starts as razorpay-only so the step is never briefly empty
	// while the lookup is in flight, and stays that way if the lookup fails.
	availableProviders: ["razorpay"],
	payErr: "",
	payBusy: false,
	// True when reconcile landed us directly on "connect" (signup + payment
	// completed in an earlier session): there is no local plan/email/company
	// context, so Back to Review & Pay is hidden (it would re-run start_signup
	// with empty args).
	reconciledConnect: false,
	successData: null,
	// provisioning gate: after pay, the openclaw container is still spinning up.
	// We block entry to the Connect step until it's running (else save_llm_pool
	// has no container to configure).
	provisioning: false,
	provisionErr: "",
	// post-save readiness recheck (Connect + self-host both funnel through
	// afterSaveRecheckReady/forceContinue below). finishSubtitle swaps the
	// spinner's default line for a calm "this can take a few minutes" message
	// once the sync is confirmed still-converging server-side (F2 pending).
	finishing: false,
	finishNote: "",
	finishSubtitle: "",
	// self-host (renderSelfHost / renderShResults, jarvis_onboarding.js ~296-376)
	shUrl: "",
	shToken: "",
	shStream: true,
	shDeep: false,
	shTestBusy: false,
	shTestResult: null,
	shSaveBusy: false,
	shErr: "",
	shWarning: "",
});

const steps = computed(() => (state.mode === "selfhost" ? STEPS_SELFHOST : STEPS_MANAGED));
const selectedPlan = computed(() => state.plans.find((p) => p.name === state.planName) || {});
const railIndex = computed(() => RAIL.findIndex((r) => r.id === state.step));
const frameSub = computed(() => FRAME_SUBS[state.step] || "Set up your workspace");

// No "Popular" tag: the admin plan catalog carries no recommended/popular
// flag, and fabricating one positionally (e.g. always the middle card) would
// mislabel whatever plan happens to sit there. Reintroduce only if the
// catalog grows a real flag.

// Auto-pay trial: a paid plan with a trial window. Checkout collects the
// autopay mandate now; the first charge fires when the trial ends.
const trialDays = computed(() => Number(selectedPlan.value.trial_days) || 0);
const isTrialPlan = computed(() => trialDays.value > 0);

// The chooser is only a choice when there is more than one gateway. With a
// single enabled gateway a radiogroup of one is noise: it asks the customer to
// decide something already decided, and the "Secured by X" line below already
// names it. Free/trial plans collect no payment at all.
const providerChoices = computed(() => state.availableProviders || []);
// Two terms the old template carried are gone, both leftovers of features that
// were removed (the free plan; dev-signup/sandbox mode) whose identifiers no
// longer exist anywhere:
//
//   isFreePlan       never defined. Harmless in a template - Vue resolves an
//                    unknown identifier to undefined and only warns, so
//                    `!isFreePlan` was permanently true - but a hard
//                    ReferenceError once moved into a computed, which is how
//                    the dead condition finally surfaced.
//   state.devActive  never assigned. Reads as undefined rather than throwing,
//                    so it silently never fired.
//
// Dropped rather than carried forward: a condition that cannot fire reads as a
// rule someone still has to reason about.
//
// Shown for ONE gateway too, not only for a choice. An earlier version hid the
// row entirely below two options, reasoning that a radiogroup of one is a fake
// decision. That reasoning was right but discarded the wrong half: the customer
// still needs to see who is about to take their money, and a line of small text
// is weaker assurance than the brand they are about to be handed to. A single
// gateway therefore renders as a NON-INTERACTIVE chip - present and legible,
// with nothing to decide.
const showProviderChooser = computed(
	() => !isTrialPlan.value && providerChoices.value.length >= 1
);
const isSingleProvider = computed(() => providerChoices.value.length === 1);
const providerAvailable = (p) => providerChoices.value.includes(p);
// Clicking is only meaningful when there is something to switch to. Guarding
// here as well as via :disabled keeps the selection honest even if the chip is
// reached some other way (keyboard, a stray programmatic click).
function chooseProvider(p) {
	if (isSingleProvider.value || !providerAvailable(p)) return;
	state.paymentProvider = p;
}

// Ask the control plane which gateways are live and preselect its default.
// Fail-open and non-blocking: the wizard must render even if this never
// answers, and the razorpay-only seed above is the floor.
async function loadPaymentProviders() {
	try {
		const r = (await listPaymentProviders()) || {};
		const providers = Array.isArray(r.providers) ? r.providers.filter(Boolean) : [];
		if (!providers.length) return;
		state.availableProviders = providers;
		// Preselect admin's default; never leave the selection pointing at a
		// gateway that is no longer offered, or Pay would post a provider the
		// server refuses.
		const preferred = providers.includes(r.default) ? r.default : providers[0];
		if (!providers.includes(state.paymentProvider)) state.paymentProvider = preferred;
		else if (providers.length === 1) state.paymentProvider = providers[0];
	} catch (e) {
		// Keep the seed: a control-plane blip must not block payment entirely.
	}
}

// Pay CTA copy: "Start free trial" for an autopay trial (nothing due
// today); "Pay ₹X" for a plain paid plan.
const payCta = computed(() => {
	if (isTrialPlan.value) return "Start free trial";
	return `Pay ${inr(selectedPlan.value.price_inr)}`;
});

// Review-card labels (preview .rev): "Pro · Monthly" plan row and a plain
// amount in the emphasized total row.
const planRowLabel = computed(() => {
	const p = selectedPlan.value;
	if (!p.plan_name) return "";
	return p.billing_cycle ? `${p.plan_name} · ${p.billing_cycle}` : p.plan_name;
});
const dueTodayLabel = computed(() => {
	if (isTrialPlan.value)
		return `₹0 · then ${inr(selectedPlan.value.price_inr)}${
			planSuffix(selectedPlan.value.price_inr, selectedPlan.value.billing_cycle) || ""
		} after ${trialDays.value} days`;
	return planAmount(selectedPlan.value.price_inr);
});

function goNext() {
	state.step = nextStep(steps.value, state.step);
}
function goBack() {
	state.step = prevStep(steps.value, state.step);
}
// Intro tour exits (CTA / advancing past the last slide / Skip tour) all land
// on the Plan step.
function startWizard() {
	state.step = "plan";
}
// Self-host is a side branch off the Plan step, not a rail step. Entering and
// leaving it flips `state.mode` so `steps` (and goNext from a reconciled
// selfhost resume) stay coherent.
function enterSelfhost() {
	state.mode = "selfhost";
	state.step = "selfhost";
}
function backFromSelfhost() {
	state.mode = "managed";
	state.step = "plan";
}

// ---- on-mount reconcile: resume a mid-flight signup ------------------------
// A mid-flight signup must land on the right step, NOT the intro tour - the
// tour shows only for a fresh, not-started onboarding (the default "intro"
// step stands when nothing below matches).
//
// Best-effort reconcile: use is_ready_for_chat's `reason` to pick the right
// track/step, then (for the managed "signup not done yet" case) poll
// check_signup_payment_state to see whether there's a live order/verification
// to resume. Fails open on any error (no admin URL configured yet, not a
// System Manager, admin API unreachable are all expected on a genuine first
// run) - falls back to the default "intro" step.
//
// check_signup_payment_state is, on desk, ONLY ever called from the "check
// your email" screen (renderVerifyEmail's "I've verified" button,
// jarvis_onboarding.js ~1612) - never from a fresh pay-review screen. So
// EITHER truthy result here (a live razorpay_order_id, or still-
// pending_verification) maps to that same desk sub-screen, not to the review
// screen (which would re-call start_signup - untested for idempotency and not
// a real desk code path). onVerifyCheck() below re-polls
// check_signup_payment_state itself and branches on the same two fields, so
// landing here in "verify" phase re-derives the correct next action either
// way. Known gap: email/company/plan text are blank on a resumed session
// (never persisted) until the customer re-verifies - cosmetic only.
async function reconcileMidFlightSignup() {
	try {
		const ready = await isReadyForChat();
		if (ready && ready.reason === "selfhost_connection" && canSelfHost) {
			// Self-host connect is SM-only; a Jarvis-Admin-not-SM must not be
			// routed into the selfhost step (its save 403s). Fall through to the
			// default step for them.
			state.mode = "selfhost";
			state.step = "selfhost";
			return;
		}
		if (
			ready &&
			(ready.reason === "llm_credentials" || ready.reason === "llm_pool_provisioning")
		) {
			// Signup + payment already done; only the AI connection is missing
			// (llm_credentials) or a configured pool never finished its first
			// apply (llm_pool_provisioning) - both resume at the connect step,
			// whose sync-status poller shows the pending/failed state. Mark the
			// resume so the Connect step hides Back (no local signup context to
			// return to - see state.reconciledConnect).
			state.mode = "managed";
			state.step = "connect";
			state.reconciledConnect = true;
			return;
		}
		// reason === "signup" (or call failed) - no completed signup yet, but
		// one may still be mid-flight (started, awaiting verification/payment).
		//
		// The in-flight test goes through verifyPollAction rather than naming
		// order ids here. This gate used to check razorpay_order_id only, which
		// made it blind to BOTH Cashfree shapes - and a Cashfree mandate is
		// exactly the case that needs it, because authorising one is a full-page
		// redirect that lands back here with the wizard's in-memory state gone.
		// Falling through dropped the customer at "intro", as if they had never
		// signed up, moments after they authorised. verifyPollAction is the one
		// place that knows every gateway/shape, so the two cannot drift again.
		const pay = await checkSignupPaymentState();
		if (pay && (pay.pending_verification || verifyPollAction(pay).kind === "checkout")) {
			state.mode = "managed";
			state.step = "pay";
			state.payPhase = "verify";
		}
		// else: nothing in flight - leave the default "intro" step (fresh start).
	} catch (e) {
		// Fail-open - never block the wizard from rendering.
	}
}

// ---- Plan (Choose Your Plan) ------------------------------------------------
async function loadPlans() {
	state.plansErr = "";
	state.plansLoading = true;
	try {
		state.plans = (await listPlans()) || [];
	} finally {
		state.plansLoading = false;
	}
}
// Error-surfacing wrapper shared by the step-entry watch, the Retry button on
// a failed load, and the intro-tour prefetch.
function loadPlansSafe() {
	loadPlans().catch((e) => {
		state.plansErr = errMsg(e);
	});
}
// Feature list parsing matches desk's renderPlan card body verbatim.
function planFeatures(p) {
	return String((p && p.features) || "")
		.split(/\r?\n/)
		.map((s) => s.trim())
		.filter(Boolean);
}
// The price renders as a big amount with a small muted "/mo" suffix
// (₹3,999 <span>/mo</span>) via the shared planAmount/planSuffix helpers
// from account/format.js (same semantics as planPriceLabel there).
// Cycle line under the price ("Billed monthly" / "Billed annually"), keyed
// off the shared suffix helper so the cycle rule can't drift.
function planCycleLabel(p) {
	const suffix = planSuffix(p && p.price_inr, p && p.billing_cycle);
	const trial = Number(p && p.trial_days) || 0;
	const billed = suffix === "/yr" ? "Billed annually" : "Billed monthly";
	// Auto-pay trial: nothing charged until the trial ends, then auto-pay begins.
	return trial > 0 ? `${trial}-day free trial, then ${billed.toLowerCase()}` : billed;
}
function onPlanContinue() {
	if (!state.planName) return;
	goNext();
}

// ---- Details (Your Details) -------------------------------------------------
// Validation matches the old Account step verbatim: email regex + non-empty
// company. The contact/billing fields are collected but not (yet) submitted -
// see the TODO(backend) note on `state` above. Until the backend accepts
// them, they're persisted to localStorage on submit so they survive reloads
// and can be backfilled once the signup contract carries them.
const BILLING_LS_KEY = "jarvis-onboarding-billing";
function persistBillingDetails() {
	try {
		window.localStorage.setItem(
			BILLING_LS_KEY,
			JSON.stringify({
				contact: state.contact,
				billingAddress: state.billingAddress,
				city: state.city,
				gstin: state.gstin,
			})
		);
	} catch (e) {
		/* storage full/blocked - purely best-effort */
	}
}
// Restore on mount; never overwrites something the user already typed.
function restoreBillingDetails() {
	try {
		const d = JSON.parse(window.localStorage.getItem(BILLING_LS_KEY) || "{}");
		if (d.contact && !state.contact) state.contact = d.contact;
		if (d.billingAddress && !state.billingAddress) state.billingAddress = d.billingAddress;
		if (d.city && !state.city) state.city = d.city;
		if (d.gstin && !state.gstin) state.gstin = d.gstin;
	} catch (e) {
		/* corrupt entry - ignore */
	}
}
function onDetailsSubmit() {
	state.detailsErr = "";
	state.email = (state.email || "").trim();
	state.company = (state.company || "").trim();
	if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(state.email)) {
		state.detailsErr = "Enter a valid email address.";
		return;
	}
	if (!state.company) {
		state.detailsErr = "Company name is required.";
		return;
	}
	persistBillingDetails();
	// Entering Review & Pay fresh from Details: reset the pay sub-state.
	state.payPhase = "review";
	state.payErr = "";
	goNext();
}

// ---- Pay (renderPay / renderVerifyEmail / startPay / openCheckout,
// jarvis_onboarding.js ~515 & ~1575-1682) ------------------------------------
// Signup fires at the Details → Pay boundary via this step's single CTA:
// the customer reviews, clicks once, and that click runs startSignup →
// verify-email/checkout branches EXACTLY as the desk flow does. start_signup
// is deliberately NOT fired on step entry: it is not idempotent-tested, and
// Back-then-Continue would re-call it.

// Lazy-load the Razorpay Checkout script (mirrors desk's page-load
// `frappe.require("https://checkout.razorpay.com/v1/checkout.js")`, ~line 18)
// as a promise so openCheckout() can await it instead of racing window.Razorpay.
let razorpayLoadPromise = null;
function ensureRazorpayLoaded() {
	if (window.Razorpay) return Promise.resolve();
	if (razorpayLoadPromise) return razorpayLoadPromise;
	razorpayLoadPromise = new Promise((resolve, reject) => {
		const s = document.createElement("script");
		s.src = "https://checkout.razorpay.com/v1/checkout.js";
		s.onload = () => resolve();
		s.onerror = () => {
			razorpayLoadPromise = null;
			reject(new Error("Couldn't load the Razorpay checkout script."));
		};
		document.head.appendChild(s);
	});
	return razorpayLoadPromise;
}

// Preload the checkout script on entering the Pay step (harmless if unused).
async function enterPayStep() {
	ensureRazorpayLoaded().catch(() => {
		/* surfaced later if actually needed */
	});
}

// Click handler for the Pay button.
async function onPayClick() {
	state.payErr = "";
	// Guard against a signup with empty args: on a reconciled resume (or any
	// state loss) there is no local plan/email/company, and startSignup(email,
	// company, null) would create a broken signup upstream.
	if (!state.planName || !state.email || !state.company) {
		state.payErr =
			"Your signup details are missing. Please go back and pick a plan and enter your details again.";
		return;
	}
	state.payBusy = true;
	await runStartPay();
}

function _sleep(ms) {
	return new Promise((r) => setTimeout(r, ms));
}

// Provisioning gate: after pay, the openclaw container is still spinning up.
// Don't enter Connect until it's running - otherwise save_llm_pool there has
// no container to configure. If pay already returned a running tenant, advance
// immediately; otherwise poll sync_connection until the container is ready.
async function proceedAfterPay() {
	const sd = state.successData || {};
	if (sd.agent_url || sd.tenant_status === "running") {
		goNext();
		return;
	}
	state.provisioning = true;
	state.provisionErr = "";
	for (let i = 0; i < 45; i++) {
		// ~45 × 2s ≈ 90s
		try {
			const r = await syncConnection();
			if (r && (r.synced || r.tenant_status === "running")) {
				state.provisioning = false;
				goNext();
				return;
			}
		} catch (e) {
			/* transient admin/agent hiccup - keep polling */
		}
		await _sleep(2000);
	}
	state.provisioning = false;
	state.provisionErr =
		"Your workspace is still being set up. This can take a minute. Retry when you're ready.";
}

async function runStartPay() {
	try {
		const d = await startSignup(
			state.email,
			state.company,
			state.planName,
			state.paymentProvider
		);
		if (d && d.pending_verification) {
			state.payPhase = "verify";
			state.payBusy = false;
			return;
		}
		await launchCheckout(d);
	} catch (e) {
		state.payBusy = false;
		state.payErr = errMsg(e);
	}
}

// "I've verified my email" click handler: re-poll check_signup_payment_state
// and branch via verifyPollAction (steps.js - pure + unit-tested). Paid plans
// continue to Razorpay Checkout; free/trial plans are already Active after
// the email click (verification IS the whole signup), so they skip payment
// and go straight to the provisioning gate - the poll response also carried
// customer_password, which the bench endpoint already persisted.
async function onVerifyCheck() {
	state.payErr = "";
	state.payBusy = true;
	try {
		const d = await checkSignupPaymentState();
		const action = verifyPollAction(d);
		if (action.kind === "checkout") {
			await launchCheckout(d, action.provider);
			return;
		}
		if (action.kind === "complete") {
			// No connection handles in this response - proceedAfterPay polls
			// sync_connection until the container is assigned + running.
			state.successData = d || {};
			state.payBusy = false;
			await proceedAfterPay();
			return;
		}
		state.payBusy = false;
		if (action.kind === "wait") {
			state.payErr =
				"We haven't received your verification yet. Click the link in your email, then try again.";
		} else if (action.kind === "halted") {
			state.payErr = `This signup is ${action.status.toLowerCase()} and can't continue. Start a new signup or contact support.`;
		} else {
			state.payErr = "Signup state has changed. Refresh this page to continue.";
		}
	} catch (e) {
		state.payBusy = false;
		state.payErr = errMsg(e);
	}
}

// Cashfree Checkout v3 SDK, DOM-injected at runtime (mirrors ensureRazorpayLoaded)
// so it stays out of the self-contained SPA bundle.
let cashfreeLoadPromise = null;
function ensureCashfreeLoaded() {
	if (window.Cashfree) return Promise.resolve();
	if (cashfreeLoadPromise) return cashfreeLoadPromise;
	cashfreeLoadPromise = new Promise((resolve, reject) => {
		const s = document.createElement("script");
		s.src = "https://sdk.cashfree.com/js/v3/cashfree.js";
		s.onload = () => resolve();
		s.onerror = () => {
			cashfreeLoadPromise = null;
			reject(new Error("Couldn't load the Cashfree checkout script."));
		};
		document.head.appendChild(s);
	});
	return cashfreeLoadPromise;
}

// Provider dispatcher: the admin response (or the verify-poll action) carries a
// payment_provider discriminator; launch the matching gateway. Defaults to
// razorpay so nothing changes when admin returns no discriminator.
function launchCheckout(d, provider) {
	const p = (provider || (d && d.payment_provider) || "razorpay").toLowerCase();
	if (p === "cashfree") return openCashfreeCheckout(d);
	return openRazorpayCheckout(d);
}

// Cashfree checkout. Unlike Razorpay there is NO client-side signature: after
// the modal we confirm SERVER-SIDE by polling finish_payment, which makes admin
// fetch the real order status from Cashfree. A forged "success" can't activate.
async function openCashfreeCheckout(d) {
	try {
		await ensureCashfreeLoaded();
	} catch (e) {
		state.payBusy = false;
		state.payErr = "Couldn't load the payment form. Check your connection and try again.";
		return;
	}
	state.payBusy = false;
	let cf;
	try {
		cf = window.Cashfree({ mode: d.cashfree_env === "production" ? "production" : "sandbox" });
	} catch (e) {
		state.payErr = "Couldn't start Cashfree checkout.";
		return;
	}
	// Paid MONTHLY plans authorize a recurring mandate instead of paying an
	// order, and the two are NOT interchangeable: the mandate carries a
	// subscription_session_id and needs subscriptionsCheckout({subsSessionId}).
	// Passing it to checkout({paymentSessionId}) fails.
	if (d.subscription_session_id) {
		return openCashfreeMandate(cf, d);
	}

	try {
		// _modal keeps the SPA mounted (a full redirect would tear down wizard state).
		await cf.checkout({ paymentSessionId: d.payment_session_id, redirectTarget: "_modal" });
	} catch (e) {
		// modal error / user close: fall through to the confirm poll - the payment
		// may still have succeeded, and the server-side poll is what decides.
	}
	state.payBusy = true;
	await confirmCashfree(d.cashfree_order_id);
}

// Mandate authorization (autopay monthly). This is a REDIRECT journey, not the
// order flow's modal, and that is imposed by the SDK rather than chosen:
// subscriptionsCheckout POSTs a form whose target is the raw redirectTarget, so
// "_modal" would open a window literally named _modal instead of an overlay.
// It also resolves the moment the form is submitted - {redirect:true} - so
// there is nothing to await and no result to inspect.
//
// The customer therefore leaves the wizard. Admin sets Cashfree's return_url to
// this page, and on the way back the wizard's normal resume path
// (verifyPollAction -> finish_payment) confirms the mandate server-side, exactly
// as it already does after email verification. Nothing trusts the redirect
// itself: confirm refuses any mandate Cashfree does not report ACTIVE.
async function openCashfreeMandate(cf, d) {
	try {
		const r = await cf.subscriptionsCheckout({
			subsSessionId: d.subscription_session_id,
			redirectTarget: "_self",
		});
		// The SDK resolves with {error} rather than throwing for bad input.
		if (r && r.error) {
			state.payBusy = false;
			state.payErr = "Couldn't start the auto-pay authorisation. Try again.";
		}
	} catch (e) {
		state.payBusy = false;
		state.payErr = "Couldn't start the auto-pay authorisation. Try again.";
	}
}

// Poll finish_payment (→ admin confirm_payment → Cashfree Get Order/Payments).
// Succeeds once Cashfree reports the order PAID (sync confirm or the webhook,
// whichever lands first); both converge idempotently on activation.
async function confirmCashfree(cashfree_order_id) {
	for (let i = 0; i < 12; i++) {
		try {
			const rr = await finishPayment({ provider: "cashfree", cashfree_order_id });
			state.successData = rr;
			state.payBusy = false;
			await proceedAfterPay();
			return;
		} catch (e) {
			// Not confirmed yet (order not PAID, or a transient) - wait and retry.
			await _sleep(3000);
		}
	}
	state.payBusy = false;
	state.payErr =
		"We couldn't confirm your payment yet. If you completed it, it'll finalize shortly — refresh in a moment.";
}

// Razorpay Checkout - options object + success handler ported verbatim from
// desk openCheckout (jarvis_onboarding.js ~1646-1676). See task-4-report.md
// for the field-by-field comparison against the desk source.
async function openRazorpayCheckout(d) {
	try {
		await ensureRazorpayLoaded();
	} catch (e) {
		state.payBusy = false;
		state.payErr = "Couldn't load the payment form. Check your connection and try again.";
		return;
	}
	state.payBusy = false;
	// Two Checkout modes sharing one options object: a one-shot order
	// (order_id) or an autopay-trial mandate authorization (subscription_id -
	// Razorpay collects the recurring-payment consent; the first charge fires
	// at the trial's end, server-side). The success payload mirrors the mode:
	// order checkouts return razorpay_order_id, subscription checkouts return
	// razorpay_subscription_id; finishPayment forwards whichever is present.
	const rzOpts = {
		key: d.razorpay_key_id,
		name: agentName,
		description: d.razorpay_subscription_id
			? `${agentName} subscription (auto-pay after trial)`
			: `${agentName} subscription`,
		handler: (res) => {
			state.payBusy = true;
			finishPayment({
				razorpay_payment_id: res.razorpay_payment_id,
				razorpay_order_id: res.razorpay_order_id,
				razorpay_subscription_id: res.razorpay_subscription_id,
				razorpay_signature: res.razorpay_signature,
			})
				.then((rr) => {
					state.successData = rr;
					state.payBusy = false;
					proceedAfterPay(); // gate on provisioning before → "connect"
				})
				.catch((e) => {
					state.payBusy = false;
					state.payErr = errMsg(e);
				});
		},
		// Razorpay dismiss (customer closed Checkout without paying) - same
		// message as desk's modal.ondismiss, shown inline instead of via
		// frappe.show_alert (no toast primitive on this surface yet).
		modal: {
			ondismiss: () => {
				state.payBusy = false;
				state.payErr = isTrialPlan.value
					? "Authorization cancelled. Click Start free trial to try again."
					: "Payment cancelled. Click Pay to try again.";
			},
		},
	};
	if (d.razorpay_subscription_id) rzOpts.subscription_id = d.razorpay_subscription_id;
	else rzOpts.order_id = d.razorpay_order_id;
	const rz = new window.Razorpay(rzOpts);
	rz.open();
}

// ---- post-save readiness recheck (Connect + self-host) ----------------------
// CRITICAL: the router's first-run guard (router/index.js) caches its
// is_ready_for_chat probe in a module-level `readyPromise` for the lifetime
// of the page - it never invalidates mid-session. So a plain
// `router.push({ name: "Chat" })` right after completing onboarding would
// read that STALE "not ready" cache and bounce straight back to
// /onboarding. Both completion paths (onConnected below and
// onSelfHostSave) instead do a FULL PAGE RELOAD via
// window.location.assign("/jarvis/") once ready, which re-imports the
// router module from scratch and re-runs the readiness check fresh.
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Defect-2 fix (2026-07-23 out-of-quota trace): this used to be 5 attempts * 800ms -
// about 4 SECONDS - while waitForSyncTerminal below budgets up to 15 MINUTES for the
// same save's provisioning job. That asymmetry was never intentional; it is a leftover
// from when the sync job's terminal flip and admin's chat-readiness verdict
// (_admin_chat_gate in jarvis/account.py, the FINAL check inside is_ready_for_chat)
// were assumed to settle in lockstep. In practice the chat-readiness verdict is a
// SEPARATE admin round-trip that can lag the sync job's own terminal state by tens of
// seconds, so a 4s budget almost always timed out before the real verdict - ready, or
// a specific not-ready reason + detail - ever came back, and the customer got a
// made-up generic sentence instead of the truth admin already knew. ~75s gives that
// final check room to actually catch up, without making a genuinely-stuck customer
// wait dramatically longer than before.
const READINESS_POLL_ATTEMPTS = 30;
const READINESS_POLL_INTERVAL_MS = 2500;

// Poll is_ready_for_chat a few times (short backoff) rather than trusting a
// single check - the save itself (pool save or self-host connect) can
// return before whatever it kicked off (e.g. proxy provisioning) is fully
// reflected. Fails closed (ready:false) on a persistent error; callers treat
// "not ready yet" as advisory, not fatal - see finishNote below.
//
// Defect-1 fix: records the LAST OBSERVED {reason, detail} even on a losing poll,
// instead of collapsing the whole result to a bare boolean - the caller needs the
// real reason to tell the customer the truth (e.g. "Your OpenAI account has reached
// its usage limit...") rather than a generic "still finishing" shrug. Only a THROWN
// error (network hiccup) is swallowed and ignored; a RETURNED {ready:false, ...} is a
// real verdict and overwrites whatever came before it.
async function waitUntilReady(
	attempts = READINESS_POLL_ATTEMPTS,
	delayMs = READINESS_POLL_INTERVAL_MS
) {
	let last = { reason: null, detail: "" };
	for (let i = 0; i < attempts; i++) {
		try {
			const r = await isReadyForChat();
			if (r && r.ready) return { ready: true, reason: null, detail: "" };
			if (r) last = { reason: r.reason || null, detail: r.detail || "" };
		} catch (e) {
			// keep retrying - transient network hiccups shouldn't strand the user
		}
		if (i < attempts - 1) await sleep(delayMs);
	}
	return { ready: false, ...last };
}

// Manual fallback for the "still not ready" case: never hard-block. The
// customer can always force their way to Chat; if something's genuinely
// still missing, Chat/Account will surface that.
function forceContinue() {
	window.location.assign("/jarvis/");
}

// First-time provisioning runs in a background job whose budget is minutes
// (cold container provision + proxy sidecars), not seconds. Readiness only
// flips once that job APPLIES the pool, so before probing is_ready_for_chat
// we follow the job itself: poll get_llm_sync_status until it leaves
// "pending:". The ceiling is a UX bound, not a correctness guarantee: it
// clears the backend's 600s job envelope (ADMIN_SYNC_RQ_TIMEOUT_S) plus one
// lock-loss retry hop; a pathological retry chain can honestly outlast it,
// in which case the caller falls through to the "still finishing" note with
// a manual continue - never a hard block. Returns the terminal sync dict,
// or null on timeout.
async function waitForSyncTerminal(maxMs = 15 * 60 * 1000, intervalMs = 3000) {
	const deadline = Date.now() + maxMs;
	for (;;) {
		try {
			const s = await getLlmSyncStatus();
			// "pending:" (incl. "pending: admin applying config") is NOT terminal
			// - the admin persisted the config and a reconcile is finishing the
			// apply. Keep following it; surface a calm reassurance rather than the
			// red failure note. Only ok/failed (not pending) ends the loop.
			if (s && !s.pending) return s;
			if (s && s.pending) {
				state.finishSubtitle =
					"Finishing setup — this can take a few minutes. We’ll keep at it; " +
					"you can safely wait or come back.";
			}
		} catch (e) {
			// transient network hiccups shouldn't strand the user
		}
		if (Date.now() >= deadline) return null;
		await sleep(intervalMs);
	}
}

// Shared tail for both completion paths: optionally follow an in-flight
// provisioning sync to a terminal state, then poll for readiness, then
// either auto-reload (the common case) or leave a "still finishing" note
// with a manual continue button so the user is never stuck on a spinner.
//
// followSync is ONLY for the managed pool path (save_llm_pool writes a
// "pending:" status synchronously before returning, so a sync from THIS
// save is observable as pending right now). The self-host save never
// touches last_sync_status, and a no-op / container-owned managed save
// enqueues nothing - in both cases the field may hold a STALE terminal
// "failed:" (or a stale "pending:" from an abandoned earlier attempt),
// which must not block an actually-ready tenant. Hence: only follow a
// sync we can see in flight, and never gate the self-host path on this
// field at all.
async function afterSaveRecheckReady({ followSync = false } = {}) {
	state.finishNote = "";
	state.finishSubtitle = "";
	state.finishing = true;
	if (followSync) {
		// save_llm_pool writes "pending:" synchronously before its response,
		// and onConnected only fires after a successful save - so whatever
		// this probe reads is THIS save's sync: still pending (follow it to
		// terminal) or already terminal (a fast failure, e.g. an immediate
		// auth error - which must surface its actionable status, not fall
		// through to a generic "still finishing" note that hides the
		// diagnostic the status field already carries).
		let terminal = null;
		try {
			const s0 = await getLlmSyncStatus();
			terminal = s0 && s0.pending ? await waitForSyncTerminal() : s0;
		} catch (e) {
			// status probe is advisory - fall through to the readiness poll
		}
		const status = ((terminal && terminal.last_sync_status) || "").trim();
		if (status.startsWith("failed") || status.startsWith("skipped")) {
			state.finishing = false;
			// Defect fix (2026-07-23 out-of-quota trace): when the status carries a
			// real customer sentence (jarvis_settings.py now writes
			// "failed: Your OpenAI account has reached its usage limit..." for
			// this exact case), render it directly - burying "Your OpenAI account
			// has reached its usage limit. It resets in about 27 hours." inside
			// "Setup hit a problem (...)" reads as developer text a customer
			// should never have to parse. syncStatusNote keeps the wrapper for
			// statuses that are genuinely opaque ("failed: unexpected error; see
			// Error Log", "failed: auth: ...", "skipped: no longer proxy-valid...").
			// The wrapper copy itself lives in steps.js and is whitelabelled there
			// via `agentName`, so develop's branding is preserved.
			state.finishNote = syncStatusNote(status, agentName);
			return;
		}
	}
	const result = await waitUntilReady();
	if (result.ready) {
		// Keep the "Setting up Jarvis" spinner up THROUGH the full-page reload.
		// Flipping finishing off first re-shows the editor for a frame before
		// the browser navigates. Leave it on; location.assign tears the page down.
		window.location.assign("/jarvis/");
		return;
	}
	state.finishing = false;
	// Defect-1 fix: render the backend's OWN sentence (is_ready_for_chat's `detail`,
	// admin-owned wording - e.g. "Your OpenAI account has reached its usage limit. It
	// resets in about 27 hours.") instead of a made-up generic one. notReadyNote falls
	// back to the old generic copy only when the backend truly has no wording for this
	// reason yet (an older admin, or a reason account.py hasn't wired a sentence for) -
	// see steps.js, where that fallback is whitelabelled via `agentName` so develop's
	// branding survives. The "Continue to <agent>" action below now sits right next to
	// whichever of the two is showing, so it always reads as an honest choice rather
	// than an unexplained escape hatch.
	state.finishNote = notReadyNote(result.detail, agentName);
}

// ---- Connect (renders <LlmPoolEditor>) - the component itself owns
// Quick/Preset/Custom + save_llm_pool; this is only the post-save readiness
// handoff. ---------------------------------------------------------------
function onConnected(sync) {
	afterSaveRecheckReady({ followSync: true });
}

// The Connect footer (Back + Connect & Finish) lives here, not inside
// LlmPoolEditor (:footerless), so it matches every other step's footer. Save
// is triggered on the editor via its exposed save() method.
const poolRef = ref(null);
const savingConnect = ref(false);
// True once the embedded editor reports a savable config (account connected,
// or API key filled) - gates the Connect & Finish button.
const connectReady = ref(false);
async function saveConnect() {
	if (!poolRef.value) return;
	savingConnect.value = true;
	try {
		await poolRef.value.save();
	} finally {
		savingConnect.value = false;
	}
}

// ---- Self-host (renderSelfHost / renderShResults / runSelfHostTest /
// saveSelfHost, jarvis_onboarding.js ~296-376) --------------------------------
async function runSelfHostTest() {
	state.shErr = "";
	const url = (state.shUrl || "").trim();
	if (!url) {
		state.shErr = "Enter the openclaw URL first.";
		return;
	}
	state.shTestBusy = true;
	state.shTestResult = null;
	try {
		const r = await testSelfHostConnection({
			base_url: url,
			token: (state.shToken || "").trim(),
			deep: state.shDeep ? 1 : 0,
		});
		state.shTestResult = r || {};
	} catch (e) {
		state.shErr = errMsg(e);
	} finally {
		state.shTestBusy = false;
	}
}

async function onSelfHostSave() {
	state.shErr = "";
	state.shWarning = "";
	const url = (state.shUrl || "").trim();
	const tok = (state.shToken || "").trim();
	if (!url || !tok) {
		state.shErr = "openclaw URL and gateway token are both required.";
		return;
	}
	state.shSaveBusy = true;
	try {
		const r = await saveSelfHosted({
			base_url: url,
			token: tok,
			deep: state.shDeep ? 1 : 0,
			stream: state.shStream ? 1 : 0,
		});
		const m = r || {};
		state.shSaveBusy = false;
		if (m.ok) {
			// Advisory only (e.g. no Self-Host Tool User set yet) - the connection
			// itself is already saved, so this doesn't block the readiness recheck.
			if (m.warning) state.shWarning = m.warning;
			await afterSaveRecheckReady();
		} else {
			state.shTestResult = m.result || {};
			state.shErr = "Validation failed. Fix the checks above, then retry.";
		}
	} catch (e) {
		state.shSaveBusy = false;
		state.shErr = errMsg(e);
	}
}

// Enter-step triggers: load the plan list on reaching "plan" (first entry
// from the tour, or a "Back" from selfhost/details), and probe dev-mode +
// preload Razorpay on reaching "pay".
watch(
	() => state.step,
	(s) => {
		if (s === "plan" && !state.plans.length && !state.plansLoading) {
			loadPlansSafe();
		}
		if (s === "pay") enterPayStep();
	}
);

// Prefill the Details step from what the site already knows (caller's email +
// default/sole company; options list for several) so the customer doesn't
// retype it. Backend-sourced because the SPA has no frappe.defaults. Never
// overwrites a value the user already typed; silent on any failure.
async function prefillAccount() {
	try {
		const d = (await getAccountDefaults()) || {};
		if (d.email && !state.email.trim()) state.email = d.email;
		if (d.company && !state.company.trim()) state.company = d.company;
		if (Array.isArray(d.companies)) state.companies = d.companies;
	} catch (e) {
		/* no-op: keep the placeholders */
	}
}

onMounted(async () => {
	prefillAccount();
	restoreBillingDetails();
	loadPaymentProviders();
	await reconcileMidFlightSignup();
	// Prefetch the plan catalog behind the intro tour so the Plan step rarely
	// first-paints in its loading state. Reconciled resumes land past "plan"
	// and skip it (the step-entry watch still covers every other path).
	if (state.step === "intro" && !state.plans.length && !state.plansLoading) loadPlansSafe();
});
</script>

<style scoped>
/* Root page chrome (jv-ob-root/jv-dark/paletteVars): kept load-bearing, see the
   template comment above the root <div> for why. Its own look (page tint,
   default text color, full-height flex column) is otherwise unchanged. */
.jv-ob-root {
	min-height: 100vh;
	background: var(--surface-1);
	color: var(--text);
	display: flex;
	flex-direction: column;
	position: relative;
}

/* ---- shared step chrome: fade-in, body/head/foot, back/link nav (design.md
   §4.3). Real frappe-ui components (Button/FormControl/Checkbox/FeatherIcon)
   own their own look; this is only the layout these steps repeat. ---- */
.ob-screen {
	animation: jvObFade 0.15s ease-out;
}
@keyframes jvObFade {
	from {
		opacity: 0;
	}
	to {
		opacity: 1;
	}
}
/* min-height keeps every step's card the same size; shorter content
   top-aligns inside it. The tour matches at 604px (TourIntro.vue). */
.ob-body {
	padding: 32px 40px 28px;
	min-height: 520px;
	box-sizing: border-box;
}
.ob-head {
	text-align: center;
	margin-bottom: 24px;
}
.ob-head h1 {
	font-size: 20px; /* text-2xl, 0.1.278 */
	font-weight: 600;
	margin: 0 0 7px;
	text-wrap: balance;
}
.ob-head p {
	font-size: 14px; /* text-p-base */
	line-height: 1.5;
	color: var(--text-2);
	margin: 0;
}
.ob-foot {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 12px;
	padding: 16px 40px 22px;
	border-top: 1px solid var(--border);
}
/* Back/quiet-link nav (design.md §4.3 "back/skip are plain text links" —
   these stay plain buttons, not frappe-ui <Button>, matching OnboardingGate's
   "Switch to Desk" precedent). */
.ob-back {
	font-size: 13px;
	color: var(--text-2);
	background: none;
	border: none;
	cursor: pointer;
	font-family: inherit;
	display: inline-flex;
	align-items: center;
	gap: 4px;
	padding: 6px 8px;
	border-radius: 8px;
	transition: background-color 0.15s ease, color 0.15s ease;
}
.ob-back:hover {
	color: var(--text);
	background: var(--surface-2);
}
.ob-back:disabled {
	opacity: 0.5;
	cursor: default;
}
.ob-back:focus-visible,
.ob-link:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
/* quiet self-host link on the Plan footer — links look like links */
.ob-link {
	font-size: 12.5px;
	color: var(--text-3);
	background: none;
	border: none;
	cursor: pointer;
	font-family: inherit;
	text-decoration: underline;
	text-underline-offset: 3px;
	padding: 4px 2px;
}
.ob-link:hover {
	color: var(--text-2);
}
.ob-note {
	font-size: 12.5px;
	color: var(--text-3);
	margin-top: 14px;
	display: flex;
	align-items: center;
	gap: 10px;
	flex-wrap: wrap;
	justify-content: center;
}

/* ---- buttons (design.md §3.1): solid near-black primary, colour-shift hover
   only. .jv-ob-btn/.jv-ob-btn-grad are the LAST two hand-rolled buttons left
   in this file - every other button call site is a real frappe-ui <Button>.
   The "Start chatting" CTA (jv-ob-btn-grad) is deliberately migrated in its
   own follow-up commit; this pair stays until then. ---- */
.jv-ob-btn {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	gap: 7px;
	height: 36px;
	padding: 0 16px;
	border-radius: 8px;
	border: 1px solid transparent;
	font-family: inherit;
	font-size: 13.5px;
	font-weight: 500;
	line-height: 1;
	cursor: pointer;
	white-space: nowrap;
	background: var(--text);
	color: var(--surface);
	transition: background-color 0.15s ease, color 0.15s ease, border-color 0.15s ease;
}
.jv-ob-btn:hover:not(:disabled) {
	background: var(--text-2);
}
.jv-ob-btn:disabled {
	opacity: 0.5;
	cursor: default;
}

/* JvCombo (Company, Details step) matched to FormControl's variant="outline"
   input recipe (frappe-ui TextInput.vue) so it looks like its FormControl
   siblings — focus-within because the border belongs on the wrapper, the
   caret sits in the inner input. */
.ob-details-form :deep(.jvc-field) {
	min-height: 28px;
	padding: 0 8px;
	gap: 8px;
	border-color: var(--border-2);
	border-radius: 6px;
	font-size: 14px;
	transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.ob-details-form :deep(.jvc-field:hover) {
	border-color: var(--text-3);
}
.ob-details-form :deep(.jvc-field:focus-within),
.ob-details-form :deep(.jvc-field.jvc-open) {
	border-color: var(--text);
	box-shadow: 0 0 0 2px var(--surface-2);
}
.ob-details-form :deep(.jvc-input::placeholder) {
	color: var(--text-3);
}

@media (max-width: 820px) {
	.ob-body {
		min-height: 0;
		padding: 26px 22px 22px;
	}
	.ob-foot {
		padding: 14px 22px 20px;
		flex-wrap: wrap;
	}
	.ob-head h1 {
		font-size: 18px;
	}
}
@media (prefers-reduced-motion: reduce) {
	.ob-screen {
		animation: none;
	}
	.jv-ob-btn,
	.ob-details-form :deep(.jvc-field) {
		transition: none;
	}
}
</style>
