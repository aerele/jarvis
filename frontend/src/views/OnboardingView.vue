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
					 tour (chromeless). -->
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
									<p>{{ planSubtitle }}</p>
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
									/>Back
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
										type="text"
										variant="outline"
										label="City (optional)"
										v-model="state.city"
										placeholder="Chennai"
										autocomplete="address-level2"
										@keydown.enter="onDetailsSubmit"
									/>
									<FormControl
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
							<p
								v-if="canReconnect"
								class="mx-auto mt-5 max-w-[620px] text-center text-p-sm text-ink-gray-5"
							>
								Already subscribed and setting this site up again?
								<button
									class="ob-link"
									:disabled="state.payBusy"
									@click="startReconnect"
								>
									Reconnect instead
								</button>
								— we'll email a code to confirm it's you. Nothing to pay again.
							</p>
							<p
								v-else-if="state.reconnectNeedsCompany"
								class="mx-auto mt-5 max-w-[620px] text-center text-p-sm text-ink-gray-5"
							>
								This email already has a subscription under a different company —
								enter that company above to reconnect it instead of paying again.
							</p>
							<div class="ob-foot">
								<button class="ob-back" @click="goBack">
									<FeatherIcon
										name="chevron-left"
										class="h-3.5 w-3.5 text-ink-gray-5"
									/>Back to tour
								</button>
								<Button
									variant="solid"
									label="Continue"
									@click="onDetailsSubmit"
								/>
							</div>
						</section>

						<!-- ===== Review & Pay — the strict payment state machine (plan 02).
							 Sub-screens are driven by the machine state (pay.value), never by
							 an HTTP status or an error message. ===== -->
						<section v-else-if="state.step === 'pay'" class="ob-screen">
							<!-- Paid: the payment step is over. Receipt + workspace-setup
								 projection, rendered separately (plan 02 §Paid/provisioning).
								 Provisioning belongs to the readiness gate, so this shows status
								 only and never a payment action. -->
							<template v-if="showPaidFlash || showProvisioning">
								<div class="ob-body">
									<div class="ob-head">
										<h1>{{ paySummaryTrial ? "Free trial started" : "Payment confirmed" }}</h1>
										<p v-if="!provisioningDelayed" role="status">
											{{
												paySummaryTrial
													? "Auto-pay authorized — nothing charged until your trial ends."
													: "Payment received."
											}}
											We're preparing your {{ agentName }} workspace. This usually
											takes under a minute…
										</p>
										<p v-else role="status">
											Your workspace is taking a little longer than usual to come
											online. Your payment is complete — nothing more is owed.
										</p>
										<!-- admin's OWN sentence when it recorded the payment but the
											 allocation failed and ops were paged. It used to be
											 discarded, leaving the customer on a 90-second spinner
											 with no idea a human already knew. -->
										<p
											v-if="pay.provisioningNote"
											class="mt-1.5 text-p-sm text-ink-gray-7"
											role="status"
										>
											{{ pay.provisioningNote }}
										</p>
										<p
											v-if="setupRecheckNote"
											class="mt-1.5 text-p-sm text-ink-gray-5"
											role="status"
										>
											{{ setupRecheckNote }}
										</p>
									</div>
									<!-- Deliberately NOT aria-hidden: JvSpinner is its own status
										 region and, while provisioning runs, the only thing on screen
										 saying the workspace is still being built. -->
									<div
										v-if="!provisioningDelayed"
										class="mt-2.5 flex justify-center"
									>
										<JvSpinner :size="72" />
									</div>
								</div>
								<div v-if="provisioningDelayed" class="ob-foot justify-end">
									<Button
										variant="solid"
										:disabled="recheckingSetup"
										:loading="recheckingSetup"
										loading-text="Checking…"
										label="Check setup status"
										@click="recheckProvisioning"
									/>
								</div>
							</template>
							<!-- Email verification still pending: no payment action until the
								 link is clicked. -->
							<template v-else-if="showVerify">
								<div class="ob-body">
									<div class="ob-head">
										<h1 ref="recoveryHeading" tabindex="-1">Check your email</h1>
										<p>
											We sent a confirmation link to <b>{{ payEmail }}</b
											>. Click the link to verify your address, then come back
											here and continue.
										</p>
									</div>
									<p class="text-center text-p-sm text-ink-gray-5" role="status">
										<template v-if="payVerifyExpiry"
											>This link expires on {{ payVerifyExpiry }}. </template
										><template v-else>The link expires in 24 hours. </template>Check
										your spam folder if it doesn't arrive.
									</p>
								</div>
								<div class="ob-foot justify-end">
									<Button
										variant="solid"
										:loading="payBusyView"
										loading-text="Working…"
										label="I've verified my email"
										@click="onPayAction(A.VERIFY)"
									/>
								</div>
							</template>
							<!-- In flight: starting signup, checkout open, or confirming.
								 A bounded status region, never an indefinite spinner replacing
								 both recovery buttons (plan 02 §a11y). -->
							<template v-else-if="payBusyView">
								<div class="ob-body ob-body--center">
									<div class="ob-head">
										<h1 role="status">{{ payBusyLabel }}</h1>
									</div>
									<div class="mt-2.5 flex justify-center">
										<JvSpinner :size="56" />
									</div>
								</div>
							</template>
							<!-- Recovery: unknown / retryable / terminal / confirm-required /
								 reconnect. Coded copy + the two named recovery actions in
								 status-first order. -->
							<template v-else-if="showRecovery">
								<div class="ob-body ob-body--center">
									<div class="ob-head">
										<h1 ref="recoveryHeading" tabindex="-1">{{ payCopy.headline }}</h1>
										<p :role="recoveryRole">{{ payCopy.body }}</p>
										<!-- The captured, customer-facing detail from the thing that
											 actually failed (an ad-blocker eating the SDK, a gateway
											 that would not open). Without this the page showed only
											 the PREVIOUS code's generic copy and the real reason went
											 nowhere. -->
										<p
											v-if="payDetail"
											class="mt-1.5 text-p-sm text-ink-gray-5"
											role="status"
										>
											{{ payDetail }}
										</p>
									</div>
									<div
										v-if="paySummaryRows.length"
										class="mx-auto w-full max-w-[420px] overflow-hidden rounded-lg border border-outline-gray-1"
									>
										<div
											v-for="(row, i) in paySummaryRows"
											:key="row.label"
											class="flex items-center justify-between gap-3 px-4 py-2.5 text-p-sm"
											:class="i < paySummaryRows.length - 1 ? 'border-b border-outline-gray-1' : ''"
										>
											<span class="text-ink-gray-5">{{ row.label }}</span>
											<b class="font-medium text-ink-gray-9">{{ row.value }}</b>
										</div>
									</div>
									<p
										v-if="pay.supportOffered"
										class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
										role="status"
									>
										Still not resolved after a few checks?
										<button class="ob-link" @click="onPayAction(A.SUPPORT)">
											Contact support
										</button>
										— we'll place it for you. Please don't pay again.
									</p>
								</div>
								<div class="ob-foot">
									<!-- A way back, always. A recovery screen with only forward
										 actions - each of which the backend may have disabled - was
										 how a customer could end up on a card with nothing to press. -->
									<button
										class="ob-back"
										:disabled="checking || initiating"
										@click="goBack"
									>
										<FeatherIcon
											name="chevron-left"
											class="h-3.5 w-3.5 text-ink-gray-5"
										/>Back
									</button>
									<div class="flex items-center gap-2">
										<Button
											v-for="a in recoveryActions"
											:key="a"
											:variant="payActionVariant(a)"
											:disabled="payActionDisabled(a)"
											:loading="payActionLoading(a)"
											loading-text="Working…"
											:label="payActionLabel(a)"
											@click="onPayAction(a)"
										/>
									</div>
								</div>
							</template>
							<!-- Review (fresh start): the customer chose a plan and entered
								 details locally, and clicks once to start the signup. -->
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
									<!-- Reconnect offer, gated ONLY on the backend's can_reconnect
										 probe (C02-5): no prose matching. Shown when the control
										 plane says this (email, company) has a paid account a
										 reconnect would find. -->
									<div
										v-if="pay.canReconnect"
										class="mx-auto mt-2 max-w-[560px] text-center"
									>
										<Button
											variant="subtle"
											label="Already subscribed? Reconnect this site"
											@click="startReconnect"
										/>
										<p class="mt-1.5 text-p-sm text-ink-gray-5">
											We'll email a code to {{ payEmail }} — enter it to connect
											this site to your existing subscription. Nothing to pay
											again.
										</p>
									</div>
								</div>
								<div class="ob-foot">
									<button
										class="ob-back"
										:disabled="payBusyView"
										@click="goBack"
									>
										<FeatherIcon
											name="chevron-left"
											class="h-3.5 w-3.5 text-ink-gray-5"
										/>Back
									</button>
									<Button
										variant="solid"
										:disabled="payBusyView"
										:loading="payBusyView"
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
						<!-- ===== Reconnect an existing subscription. NOT a rail step: it is a
						     divergence out of the purchase funnel, and RAIL hides itself for any
						     step it doesn't know - so the customer stops being told they are at
						     "Pay" while reconnecting something they already paid for. ===== -->
						<section v-else-if="state.step === 'reconnect'" class="ob-screen">
							<div class="ob-body ob-body--center">
								<div class="ob-head">
									<h1>Enter your reconnect code</h1>
									<p>
										Sent to <b>{{ state.email || "your email" }}</b> — connects
										this site to your existing subscription, nothing to pay
										again.
									</p>
								</div>
								<input
									v-model="state.reconnectCode"
									class="ob-code"
									type="text"
									autocapitalize="characters"
									autocomplete="one-time-code"
									spellcheck="false"
									maxlength="9"
									aria-label="Reconnect code"
									placeholder="ABCD2345"
									@keydown.enter="submitReconnectCode"
								/>
								<p class="ob-code-note">
									<template v-if="state.reconnectResentIn > 0">
										Sent. You can resend in {{ state.reconnectResentIn }}s.
									</template>
									<template v-else>
										Didn't get it?
										<button class="ob-link" @click="resendReconnectCode">
											Resend code
										</button>
									</template>
								</p>
								<Banner v-if="state.payErr" type="error" :message="state.payErr" />
							</div>
							<div class="ob-foot">
								<button class="ob-back" @click="cancelReconnect">
									<FeatherIcon
										name="chevron-left"
										class="h-3.5 w-3.5 text-ink-gray-5"
									/>Back
								</button>
								<Button
									variant="solid"
									:loading="state.payBusy"
									loading-text="Working…"
									:disabled="!state.reconnectCode.trim()"
									label="Finish reconnect"
									@click="submitReconnectCode"
								/>
							</div>
						</section>

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
								<Button
									variant="solid"
									:disabled="!connectReady || savingConnect"
									:loading="savingConnect"
									loading-text="Connecting…"
									label="Start chatting"
									@click="saveConnect"
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
import { reactive, ref, computed, nextTick, onMounted, onUnmounted, watch } from "vue";
import { Button, FormControl, FeatherIcon } from "frappe-ui";
import { useJarvisTheme } from "@/theme";
import LlmPoolEditor from "@/components/LlmPoolEditor.vue";
import JvCombo from "@/components/JvCombo.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import JarvisMark from "@/components/JarvisMark.vue";
import Banner from "@/components/Banner.vue";
import TourIntro from "@/onboarding/TourIntro.vue";
import SetupNeuralNet from "@/onboarding/SetupNeuralNet.vue";
import cashfreeLogo from "@/assets/cashfree.png";
import {
	STEPS_MANAGED,
	nextStep,
	prevStep,
	notReadyNote,
	syncStatusNote,
	planSubtitleFor,
} from "@/onboarding/steps";
import { inr, planAmount, planSuffix } from "@/account/format";
import {
	isReadyForChat,
	getLlmSyncStatus,
	listPlans,
	listPaymentProviders,
	reconnectAvailable,
	startAccountReconnect,
	checkAccountReconnect,
	getAccountDefaults,
	onboardingPaymentApi,
} from "@/api";
import { errMessage as errMsg } from "@/lib/errors";
import { report as reportError } from "@/lib/errorReporter";
import { agentName } from "@/branding";
import { createPaymentFlow } from "@/onboarding/usePaymentFlow";
import { openOnboardingCheckout } from "@/onboarding/onboardingCheckout";
import {
	STATES as PAY_STATES,
	remainingCooldownSeconds,
} from "@/onboarding/paymentMachine";
import { ACTIONS, ACTION_LABELS, TONE, copyFor } from "@/onboarding/paymentCodes";

const { effectiveDark: dark, paletteVars } = useJarvisTheme();

// The 4 named wizard steps shown on the rail. The intro tour is chromeless
// (no rail entry).
const RAIL = [
	{ id: "details", label: "Details" },
	{ id: "plan", label: "Plan" },
	{ id: "pay", label: "Pay" },
	{ id: "connect", label: "Connect" },
];

// Frame subtitle next to the brand mark, mirroring the active step's title.
const FRAME_SUBS = {
	intro: "Meet your ERPNext assistant",
	plan: "Choose your plan",
	details: "Your details",
	pay: "Review & pay",
	reconnect: "Reconnect your subscription",
	connect: `Give ${agentName} a brain`,
};

// ---- step machine -----------------------------------------------------------
// `state.step` walks STEPS_MANAGED (intro → plan → details → pay → connect).
const state = reactive({
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
	// pay: the payment sub-state lives in the machine (usePaymentFlow / `pay`),
	// NOT here. These fields belong to the reconnect-code step and the review
	// card's local choices only.
	reconnectRequestId: "",
	reconnectFrom: "",
	reconnectEligible: false,
	reconnectNeedsCompany: false,
	reconnectCode: "",
	reconnectResentIn: 0,
	paymentProvider: "razorpay", // gateway chosen on Review & Pay: "razorpay" | "cashfree"
	// Gateways the operator has actually enabled, narrowed to what this build
	// can render. Starts as razorpay-only so the step is never briefly empty
	// while the lookup is in flight, and stays that way if the lookup fails.
	availableProviders: ["razorpay"],
	// payErr / payBusy drive the reconnect-code step's own error + button state
	// (the payment machine owns everything on the Pay step).
	payErr: "",
	payBusy: false,
	// True when reconcile landed us directly on "connect" (signup + payment
	// completed in an earlier session): there is no local plan/email/company
	// context, so Back to Review & Pay is hidden (it would re-run start_signup
	// with empty args).
	reconciledConnect: false,
	// post-save readiness recheck (Connect funnels through
	// afterSaveRecheckReady/forceContinue below). finishSubtitle swaps the
	// spinner's default line for a calm "this can take a few minutes" message
	// once the sync is confirmed still-converging server-side (F2 pending).
	finishing: false,
	finishNote: "",
	finishSubtitle: "",
});

const steps = computed(() => STEPS_MANAGED);
const selectedPlan = computed(() => state.plans.find((p) => p.name === state.planName) || {});
// See planSubtitleFor (onboarding/steps.js) for why this can't be hardcoded.
const planSubtitle = computed(() => planSubtitleFor(state.plans));
const railIndex = computed(() => RAIL.findIndex((r) => r.id === state.step));
// Both halves of the identity must be present before the plane can resolve an
// account: several company accounts can share one address.
const reconnectInputsReady = computed(
	() => /.+@.+\..+/.test((state.email || "").trim()) && !!(state.company || "").trim()
);
// The offer is shown ONLY when the control plane confirms this (email, company)
// has an account a reconnect would actually find. Never guessed client-side: a
// wrong guess either hides recovery from someone who needs it, or sends someone
// who has no account into a code screen no code will ever arrive for.
const canReconnect = computed(() => reconnectInputsReady.value && state.reconnectEligible);

// Debounced so typing an address doesn't call the plane per keystroke, and
// cached per (email, company) so going back and forth doesn't re-ask. Fails
// closed: any error leaves the offer hidden and the customer on the normal path.
let eligibilityTimer = null;
const eligibilityCache = new Map();
async function refreshReconnectEligibility() {
	if (!reconnectInputsReady.value) {
		state.reconnectEligible = false;
		state.reconnectNeedsCompany = false;
		return;
	}
	const key = `${state.email.trim().toLowerCase()}\u0000${state.company.trim().toLowerCase()}`;
	if (eligibilityCache.has(key)) {
		const hit = eligibilityCache.get(key);
		state.reconnectEligible = hit.eligible;
		state.reconnectNeedsCompany = hit.needsCompany;
		return;
	}
	try {
		const d = (await reconnectAvailable(state.email.trim(), state.company.trim())) || {};
		const hit = { eligible: !!d.eligible, needsCompany: !!d.needs_company };
		eligibilityCache.set(key, hit);
		state.reconnectEligible = hit.eligible;
		state.reconnectNeedsCompany = hit.needsCompany;
	} catch (e) {
		state.reconnectEligible = false;
		state.reconnectNeedsCompany = false;
	}
}
watch(
	() => [state.step, state.email, state.company],
	() => {
		if (state.step !== "details") return;
		clearTimeout(eligibilityTimer);
		eligibilityTimer = setTimeout(refreshReconnectEligibility, 500);
	},
	{ immediate: true }
);
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
// Intro tour exits (CTA / advancing past the last slide / Skip tour) all land on
// whatever follows the intro - derived, not hardcoded, so it tracks STEPS_MANAGED
// instead of silently bypassing the first real step when the order changes.
function startWizard() {
	state.step = nextStep(steps.value, "intro");
}
// ---- on-mount reconcile: resume a mid-flight signup ------------------------
// The mount contract (plan 02 §Reload and multi-tab recovery). A mid-flight
// signup must land on the right step, NOT the intro tour, and the payment
// machine already holds the authoritative sub-state - the job here is to route
// STEPS from server truth.
//
// The C02-1 correction is the whole point of the ordering below: is_ready_for_chat's
// `llm_credentials` reason is NOT "has paid". Credentials persist at signup,
// BEFORE payment, so a customer who reached the old
// `llm_credentials → connect` shortcut on that reason alone could be dropped on
// Connect while their payment was never made. So payment truth
// (get_onboarding_state, via flow.hydrate) is consulted FIRST, and only a paid
// answer lets the connect shortcut fire. Fails open (default intro) on any error.
async function reconcileMidFlightSignup() {
	let truth;
	try {
		truth = await flow.hydrate(); // {paid: true|false|null, truthKnown, notStarted}
	} catch (e) {
		return; // fail open to the intro tour
	}
	// Day one: no signup on this site. Leave the default intro tour (fresh start).
	if (truth.notStarted) return;

	if (truth.paid === true) {
		// Paid - so, and only so, the connect shortcut is allowed. Ask readiness
		// for the fine-grained "what is left": a missing AI connection resumes at
		// Connect; anything else (paid, still provisioning) shows the machine's
		// provisioning projection on the Pay step.
		let ready = null;
		try {
			ready = await isReadyForChat();
		} catch (e) {
			/* readiness is advisory here - fall through to the pay projection */
		}
		if (ready && (ready.reason === "llm_credentials" || ready.reason === "llm_pool_provisioning")) {
			state.reconciledConnect = true;
			state.step = "connect";
			return;
		}
		// Paid but not yet chat-ready (container provisioning): land on Pay, where
		// the machine renders the paid receipt + "preparing your workspace".
		state.step = "pay";
		return;
	}

	// Not paid, or payment truth could not be established. Either way the connect
	// shortcut must NOT fire - "has credentials" is not "has paid". If hydrate()
	// absorbed a real payment state (verification / unknown / failed /
	// confirm_required / reconnect), render it on the Pay step; the reconnect
	// state shows its offer there too. A customer who has genuinely not started
	// anything leaves the machine on REVIEW and stays on the intro tour.
	if (pay.value.value !== S.REVIEW) {
		state.step = "pay";
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
	// Entering Review & Pay fresh from Details: the payment machine owns the pay
	// sub-state now, and a fresh review renders from its REVIEW state. Clear the
	// reconnect-code step's own error surface so a stale one does not linger.
	state.payErr = "";
	goNext();
}

// ---- Pay: the strict payment state machine (plan 02) ------------------------
// The wizard consumes @/onboarding/usePaymentFlow - a pure reducer
// (paymentMachine) plus an orchestrator that owns every side effect - instead of
// the old tangle of payPhase/successData/provisioning flags and inline checkout
// code. The machine keys on the backend's CODE, never on HTTP status prose, and
// its ONE invariant is that nothing but an authoritative paid answer leaves the
// Pay page. Checkout sheets are opened through the SHIPPED billing layer
// (onboardingCheckout → billingCheckout/useRazorpay/useCashfree), not a second
// orchestrator here.

// The Cashfree autopay MANDATE opener: a full-page subscriptionsCheckout
// redirect, the one shape the Account billing page refuses and the wizard owns.
// The order/Razorpay openers load their own SDKs; this one lives here because it
// is the view that owns the return journey - admin points Cashfree's return_url
// at jarvis_pay_return, which lands back on this wizard and resumes via the
// mount reconcile below.
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
async function openCashfreeMandate(handles) {
	await ensureCashfreeLoaded();
	const cf = window.Cashfree({
		mode: handles.cashfree_env === "production" ? "production" : "sandbox",
	});
	// {redirect:true}; resolves the moment the form submits, so there is nothing
	// to await and the browser is leaving. The SDK resolves with {error} rather
	// than throwing for bad input.
	const r = await cf.subscriptionsCheckout({
		subsSessionId: handles.subscription_session_id,
		redirectTarget: "_self",
	});
	if (r && r.error) throw new Error("Couldn't start the auto-pay authorisation. Try again.");
	return { status: "redirected" };
}

const flow = createPaymentFlow({
	api: onboardingPaymentApi,
	openCheckout: (handles, opts) =>
		openOnboardingCheckout(handles, { ...opts, openMandate: openCashfreeMandate }),
});
// The reactive machine state the template renders from.
const pay = computed(() => flow.state.value);

// ---- pay-step view model ----------------------------------------------------
const S = PAY_STATES;
const A = ACTIONS;
// The coded copy for the current state (headline, body, tone, actions), with the
// reconciliation flag switching the pending row to its money-parked variant.
const payCopy = computed(() =>
	copyFor(pay.value.code, { awaitingReconciliation: pay.value.awaitingReconciliation })
);

// Server-truth identity for the resumed/recovery screens (C02-3: never the
// prefill). Falls back to the locally typed email on the fresh review path.
const payEmail = computed(() => pay.value.summary?.email || state.email || "your email");
const paySummaryTrial = computed(() => (pay.value.summary?.trialDays || 0) > 0);
// The verification link's expiry, rendered plainly when the backend sends one so
// the page can say how long is left instead of "check your email" forever.
const payVerifyExpiry = computed(() => {
	const raw = pay.value.verificationExpiresAt;
	if (!raw) return "";
	// Admin sends a Frappe datetime ("YYYY-MM-DD HH:mm:ss[.ffffff]"). Show the
	// date portion; a full localisation is not worth a dependency here.
	return String(raw).slice(0, 16).replace("T", " ");
});

// The resumed-summary rows on a recovery card: provider, amount, a masked intent
// reference where safe, and the last time we actually checked (plan 02 §Unknown/
// failed). Read from the machine (server truth), and last-checked from `data`,
// never the persisted context (which keeps a stale stamp by design).
const paySummaryRows = computed(() => {
	const s = pay.value.summary || {};
	const rows = [];
	if (pay.value.provider) {
		rows.push({ label: "Payment method", value: pay.value.provider === "cashfree" ? "Cashfree" : "Razorpay" });
	}
	if (s.dueTodayInr != null && !Number.isNaN(s.dueTodayInr)) {
		rows.push({ label: "Amount", value: paySummaryTrial.value ? "₹0 today" : inr(s.dueTodayInr) });
	}
	const ref = maskedIntentRef.value;
	if (ref) rows.push({ label: "Reference", value: ref });
	if (pay.value.lastCheckedAt) {
		rows.push({ label: "Last checked", value: relativeSince(pay.value.lastCheckedAt) });
	}
	return rows;
});
// A short, safe intent reference: the attempt id's tail, never a gateway order id
// or a document name (those stay on admin's side).
const maskedIntentRef = computed(() => {
	const id = pay.value.attemptId || "";
	if (!id) return "";
	return id.length > 6 ? `…${id.slice(-6)}` : id;
});
function relativeSince(ts) {
	const t = Date.parse(String(ts).replace(" ", "T"));
	if (Number.isNaN(t)) return "just now";
	const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
	if (secs < 60) return "just now";
	const mins = Math.round(secs / 60);
	if (mins < 60) return `${mins} min ago`;
	const hrs = Math.round(mins / 60);
	if (hrs < 24) return `${hrs} h ago`;
	return `${Math.round(hrs / 24)} d ago`;
}

// "Check setup status" from the provisioning_delayed projection. The poll is a
// 90-second loop, so the button guards itself: the machine state does not change
// while it runs, and without this every impatient click spawned another
// concurrent loop (the flow now refuses re-entry too - this is the visible half).
const recheckingSetup = ref(false);
const setupRecheckNote = ref("");
async function recheckProvisioning() {
	if (recheckingSetup.value) return;
	recheckingSetup.value = true;
	setupRecheckNote.value = "";
	try {
		const out = await flow.waitForProvisioning();
		if (out.status === "ready") {
			state.step = "connect";
			return;
		}
		// Do not discard the outcome: a silent 90 seconds followed by the same
		// screen reads as a broken button.
		setupRecheckNote.value =
			out.status === "delayed"
				? "Still preparing your workspace. Your payment is complete — you can leave this page and come back."
				: "";
	} finally {
		recheckingSetup.value = false;
	}
}
// Sub-screen selectors. review is the fresh-start card (local plan/email chosen
// on the Details step); every other sub-screen renders from server truth in the
// machine (pay.summary), never from a prefill (C02-3).
const showVerify = computed(() => pay.value.value === S.VERIFICATION_REQUIRED);
const showProvisioning = computed(
	() => pay.value.value === S.PROVISIONING || pay.value.value === S.PROVISIONING_DELAYED
);
const showPaidFlash = computed(() => pay.value.value === S.PAID);
// The FULL-SCREEN busy view is only for the phases where there is genuinely
// nothing to press: starting the signup, the sheet being open, confirming.
// A status check or a retry deliberately does NOT hide the recovery card -
// plan 02 §a11y is explicit that both buttons must not be replaced by an
// indefinite spinner; they are disabled in place instead (see payActionDisabled).
const payBusyView = computed(
	() =>
		pay.value.value === S.STARTING_SIGNUP ||
		pay.value.value === S.CHECKOUT_OPEN ||
		pay.value.value === S.CONFIRMING
);
const checking = computed(() => pay.value.busy === "checking");
const initiating = computed(() => pay.value.busy === "initiating");
const showRecovery = computed(
	() =>
		!payBusyView.value &&
		(pay.value.value === S.UNKNOWN ||
			pay.value.value === S.FAILED_RETRYABLE ||
			pay.value.value === S.FAILED_TERMINAL ||
			pay.value.value === S.CONFIRM_REQUIRED ||
			pay.value.value === S.RECONNECT)
);
// role=alert only for an actionable failure; role=status for pending/info
// (plan 02 §a11y - a pending payment announced as an alert on every poll is a
// flashing banner to a screen reader).
const recoveryRole = computed(() => (payCopy.value.tone === TONE.ALERT ? "alert" : "status"));
// The specific, customer-facing detail the machine captured (an SDK that would
// not load, a gateway that refused to open, admin's own sentence on a coded
// refusal). Suppressed when it merely repeats the coded copy.
const payDetail = computed(() => {
	const m = (pay.value.message || "").trim();
	if (!m) return "";
	return m === payCopy.value.body || m === payCopy.value.headline ? "" : m;
});
const provisioningDelayed = computed(() => pay.value.value === S.PROVISIONING_DELAYED);

// The busy-screen line ("Confirming with Razorpay/Cashfree…").
const payBusyLabel = computed(() => {
	if (pay.value.value === S.CONFIRMING) {
		const prov = pay.value.provider === "cashfree" ? "Cashfree" : "Razorpay";
		return `Confirming with ${prov}…`;
	}
	if (pay.value.value === S.STARTING_SIGNUP) return "Starting your signup…";
	return "Opening secure checkout…";
});

// The rate-limit countdown: a live seconds value the Check button reads. Driven
// by a 1s ticker that also lets the machine re-enable the button when the
// cooldown elapses.
const nowMs = ref(Date.now());
let cooldownTimer = null;
const checkCountdown = computed(() => remainingCooldownSeconds(pay.value, nowMs.value));
const checkLabel = computed(() =>
	// The countdown wins whenever there IS one. Gating it on
	// !awaitingReconciliation && !transportError meant that a parked-payment page
	// (or one whose last check failed) that ALSO hit the rate limit showed a plain
	// "Check payment status" over a disabled button with no explanation of when it
	// would work again - which is the dead-looking-button complaint in its most
	// confusing form.
	checkCountdown.value > 0
		? `Check again in ${checkCountdown.value}s`
		: ACTION_LABELS[ACTIONS.CHECK]
);

// The action buttons for the recovery card, in the table's order (status-first).
// The support affordance is appended when the client-local check ceiling is hit
// even if the code's own actions don't list it (a pending payment the customer
// has checked many times).
const recoveryActions = computed(() => {
	const acts = [...payCopy.value.actions];
	if (pay.value.supportOffered && !acts.includes(ACTIONS.SUPPORT)) acts.push(ACTIONS.SUPPORT);
	return acts;
});

function payActionLabel(a) {
	if (a === ACTIONS.CHECK) return checkLabel.value;
	return ACTION_LABELS[a] || "";
}
function payActionDisabled(a) {
	// Any mutating call in flight disables BOTH recovery actions (plan 02: server
	// idempotency, not button state, is what prevents duplicate intents - but a
	// double-click should still not fire twice). A status check disables itself
	// and the retry, so an impatient customer cannot stack concurrent
	// provider-truth calls into the rate limit.
	if (checking.value || initiating.value) return true;
	if (a === ACTIONS.CHECK) return !pay.value.canCheck;
	if (a === ACTIONS.INITIATE) return !pay.value.canInitiate;
	return false;
}
function payActionLoading(a) {
	if (a === ACTIONS.CHECK) return checking.value;
	if (a === ACTIONS.INITIATE) return initiating.value;
	return false;
}
function payActionVariant(a) {
	// Status-first: the primary (solid) action is whichever appears first, which
	// the copy table already orders as Check where double-payment is possible.
	return recoveryActions.value[0] === a ? "solid" : "subtle";
}
async function onPayAction(a) {
	if (a === ACTIONS.CHECK) return flow.checkStatus();
	if (a === ACTIONS.INITIATE) {
		// Provider from SERVER TRUTH, not the local default. A resumed Cashfree
		// signup renders "Payment method: Cashfree" one line above this button
		// while state.paymentProvider still held the razorpay seed - and the bench
		// takes a passed provider verbatim, so the retry silently opened a second
		// live intent on a DIFFERENT gateway.
		return flow.initiatePayment({
			plan: payPlan.value,
			provider: pay.value.provider || state.paymentProvider,
		});
	}
	if (a === ACTIONS.RECONNECT) return startReconnect();
	if (a === ACTIONS.VERIFY) return flow.verifyAndContinue();
	if (a === ACTIONS.SUPPORT) {
		window.location.href = "mailto:support@aerele.in?subject=Jarvis%20onboarding%20payment";
		return;
	}
	if (a === ACTIONS.RESTART) {
		flow.cancelInFlight();
		state.step = "details";
	}
}

// The plan a retry initiates on: the one the customer chose locally, or (on a
// resumed session with no local choice) the plan NAME server truth named -
// summary.plan, not summary.planLabel, because the label is display text and the
// bench resumes on the name. Never a guess: initiating on the wrong plan is a
// wrong charge, and passing "" lets the bench fall back to its own stored
// context rather than inventing one here.
const payPlan = computed(() => state.planName || pay.value.summary?.plan || "");

// Focus management (plan 02 §Accessibility). A gateway sheet steals focus into
// its own iframe and takes it away when it closes, so a keyboard or screen-reader
// user is left with focus on nothing when the recovery screen appears. A live
// region announces the text but does not MOVE anyone - so the status heading is
// focused explicitly on each transition into a recovery/verify state.
const recoveryHeading = ref(null);
watch(
	() => pay.value.value,
	async () => {
		if (!showRecovery.value && !showVerify.value) return;
		await nextTick();
		const el = recoveryHeading.value;
		if (el && typeof el.focus === "function") el.focus();
	}
);

// Click handler for the review card's single Pay CTA. Fires start_signup exactly
// once through the flow; the machine takes it from there (verify / checkout /
// parked-money / duplicate all land on their own sub-screen).
async function onPayClick() {
	if (!state.planName || !state.email || !state.company) {
		// A signup with empty args would create a broken record upstream. This is
		// the fresh-start guard; a resumed session renders from server truth and
		// uses Initiate, not this button.
		state.detailsErr =
			"Your signup details are missing. Please go back and pick a plan and enter your details again.";
		state.step = "details";
		return;
	}
	await flow.submitReview({
		email: state.email,
		company: state.company,
		plan: state.planName,
		provider: state.paymentProvider,
	});
}

function cancelReconnect() {
	state.step = state.reconnectFrom || "pay";
	state.payErr = "";
	state.reconnectRequestId = "";
	state.reconnectCode = "";
	state.reconnectResentIn = 0;
}

// The code the customer read off the confirmation page (or got from support).
// Wrong code => admin keeps answering awaiting_code, so just say so and let
// them retype rather than restarting the whole reconnect.
// A new request supersedes the old one, so any earlier code stops working.
// Cooled down: admin allows 5 reconnect requests an hour and a frustrated
// customer would otherwise burn that budget in seconds.
async function resendReconnectCode() {
	if (state.reconnectResentIn > 0) return;
	state.payErr = "";
	try {
		const d = await startAccountReconnect(state.email, state.company);
		state.reconnectRequestId = (d && d.request) || state.reconnectRequestId;
		state.reconnectCode = "";
		state.reconnectResentIn = 30;
		const tick = setInterval(() => {
			state.reconnectResentIn -= 1;
			if (state.reconnectResentIn <= 0) clearInterval(tick);
		}, 1000);
	} catch (e) {
		state.payErr = errMsg(e);
	}
}

async function submitReconnectCode() {
	if (!state.reconnectCode.trim()) return;
	state.payErr = "";
	state.payBusy = true;
	try {
		const d = await checkAccountReconnect(
			state.reconnectRequestId,
			state.reconnectCode.trim()
		);
		if (d && d.status === "connected") {
			state.payBusy = false;
			// Reconnect rotated this site onto an EXISTING, already-paid account
			// whose container is live - so the payment machine is not involved at
			// all (the old code faked a paid transition through successData = {}).
			// Only the LLM step needs re-doing on this fresh site: go straight to
			// Connect, and mark it a reconciled resume so Connect hides Back (there
			// is no local signup context to return to).
			state.reconciledConnect = true;
			state.step = "connect";
			return;
		}
		state.payErr =
			d && d.status === "expired"
				? "The reconnect request expired. Start it again."
				: "That code didn't match. Check the confirmation page and try again.";
	} catch (e) {
		state.payErr = errMsg(e);
	} finally {
		state.payBusy = false;
	}
}

// "Already subscribed?": ask admin to mail a reconnect code, then show the
// code screen. Reachable from Details (a returning customer never has to pick a
// plan or reach a payment wall) and from a rejected pay attempt.
async function startReconnect() {
	state.payErr = "";
	state.reconnectCode = "";
	state.payBusy = true;
	// Where to return on Back/cancel: reconnect can be entered from Details
	// (before any plan is chosen), from the recovery card, and from the review
	// card's can_reconnect offer.
	state.reconnectFrom = state.step === "reconnect" ? state.reconnectFrom : state.step;
	try {
		const d = await startAccountReconnect(state.email, state.company);
		state.reconnectRequestId = (d && d.request) || "";
		state.step = "reconnect";
	} catch (e) {
		state.payErr = errMsg(e);
	} finally {
		state.payBusy = false;
	}
}

// ---- post-save readiness recheck (Connect) ----------------------------------
// CRITICAL: the router's first-run guard (router/index.js) caches its
// is_ready_for_chat probe in a module-level `readyPromise` for the lifetime
// of the page - it never invalidates mid-session. So a plain
// `router.push({ name: "Chat" })` right after completing onboarding would
// read that STALE "not ready" cache and bounce straight back to
// /onboarding. The completion path (onConnected below) instead does a FULL
// PAGE RELOAD via window.location.assign("/jarvis/") once ready, which
// re-imports the router module from scratch and re-runs the readiness check
// fresh.
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
// single check - the save itself (a pool save) can return before whatever
// it kicked off (e.g. proxy provisioning) is fully
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
// followSync is ONLY for the pool path (save_llm_pool writes a "pending:"
// status synchronously before returning, so a sync from THIS save is
// observable as pending right now). A no-op / container-owned save enqueues
// nothing, and the field may then hold a STALE terminal "failed:" (or a stale
// "pending:" from an abandoned earlier attempt), which must not block an
// actually-ready tenant. Hence: only follow a sync we can see in flight.
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
			// Error Log", "failed: auth: ...", "skipped: no longer pool-valid...").
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

// Enter-step triggers: load the plan list on reaching "plan" (first entry
// from the tour, or a "Back" from details). The checkout SDKs load themselves
// on demand (useRazorpay/useCashfree), so the Pay step no longer preloads.
watch(
	() => state.step,
	(s) => {
		if (s === "plan" && !state.plans.length && !state.plansLoading) {
			loadPlansSafe();
		}
	}
);

// Payment machine reached PAID: the readiness/connect gate owns provisioning, so
// this drives the paid→provisioning→connect handoff. flow.waitForProvisioning()
// transitions the machine into provisioning and polls sync_connection (the fenced
// former proceedAfterPay loop); on ready we advance to Connect, on a delayed
// timeout the machine stays in provisioning_delayed and the page offers "Check
// setup status". Guarded so it fires once per paid transition.
let provisioningRun = false;
watch(
	() => pay.value.value,
	async (v) => {
		if (v === PAY_STATES.PAID && !provisioningRun) {
			provisioningRun = true;
			const out = await flow.waitForProvisioning();
			if (out.status === "ready") {
				state.step = "connect";
			}
			// delayed / superseded: stay put; the machine renders the projection.
			provisioningRun = false;
		}
	}
);

// A 1s ticker for the rate-limit countdown: updates the displayed seconds and,
// when the cooldown elapses, re-enables the Check button through the machine.
watch(
	() => state.step,
	(s) => {
		if (s === "pay" && !cooldownTimer) {
			cooldownTimer = setInterval(() => {
				nowMs.value = Date.now();
				if (pay.value.checkCooldownUntil && nowMs.value >= pay.value.checkCooldownUntil) {
					flow.tickCooldown();
				}
			}, 1000);
		} else if (s !== "pay" && cooldownTimer) {
			clearInterval(cooldownTimer);
			cooldownTimer = null;
		}
	}
);
onUnmounted(() => {
	if (cooldownTimer) clearInterval(cooldownTimer);
});

// Report payment failures to the admin as they surface. The machine holds them
// as caught state (never thrown), so the global handler never sees them; this
// watcher fires on any coded failure the machine renders.
watch(
	() => [pay.value.code, pay.value.transportError, state.payErr],
	([code, transportErr, reconnectErr]) => {
		const failing =
			pay.value.value === PAY_STATES.FAILED_RETRYABLE ||
			pay.value.value === PAY_STATES.FAILED_TERMINAL ||
			transportErr;
		if (failing && code) {
			reportError({ surface: "onboarding", error_code: "payment", message: code });
		}
		// The reconnect-code step still surfaces its own errors through payErr.
		if (reconnectErr) {
			reportError({ surface: "onboarding", error_code: "reconnect", message: reconnectErr });
		}
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
/* A one-field step would otherwise pin its content to the top of the 520px
   body and leave a dead white expanse below it. */
.ob-body--center {
	display: flex;
	flex-direction: column;
	justify-content: center;
	align-items: center;
}
.ob-body--center .ob-head {
	margin-bottom: 20px;
}
/* The code IS the screen - one unmistakable target, not a small labelled
   field adrift in the middle of it. */
.ob-code {
	width: 260px;
	padding: 14px 16px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface);
	color: var(--text);
	font: 600 22px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
	letter-spacing: 6px;
	text-align: center;
	text-transform: uppercase;
	outline: none;
	transition: border-color 0.12s ease, box-shadow 0.12s ease;
}
.ob-code::placeholder {
	color: var(--text-3, #9ca3af);
	letter-spacing: 6px;
	font-weight: 500;
}
.ob-code:focus {
	border-color: var(--accent, #2563eb);
	box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
}
.ob-code-note {
	margin: 14px 0 0;
	max-width: 420px;
	text-align: center;
	font-size: 13px;
	line-height: 1.55;
	color: var(--text-2);
}
.ob-link {
	color: var(--accent, #2563eb);
	font: inherit;
	background: none;
	border: 0;
	padding: 0;
	cursor: pointer;
	text-decoration: underline;
	text-underline-offset: 2px;
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
/* quiet inline links on a step footer — links look like links */
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
	.ob-details-form :deep(.jvc-field) {
		transition: none;
	}
}
</style>
