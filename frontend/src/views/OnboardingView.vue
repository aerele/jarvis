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
					 tour (chromeless). variant="steps" opts out of the 2026-08-16
					 smooth-bar redesign - the rail is real navigation with four
					 always-visible names, not one of the wait screens that
					 redesign targeted (StepProgress.vue's own header comment). -->
					<div v-if="railIndex >= 0" class="my-4 w-full max-w-[720px]">
						<StepProgress :steps="RAIL" :current-index="railIndex" variant="steps" />
					</div>

					<div
						class="w-full overflow-hidden rounded-2xl border border-outline-gray-1 bg-surface-white shadow-2xl"
					>
						<!-- ===== Contact support: a real ticket, filed from here. Hoisted
							 above every step (not just Pay, where it started) so ANY screen
							 that offers the support action - the payment recovery/terminal/
							 maintenance-hold cards, and now the Connect step's stuck-waiting
							 card - reaches the SAME panel, and so it needs no portal: a
							 teleported dialog would lose the jv-* palette vars this view
							 binds on its own root. The action it replaces was a bare
							 mailto:, which did nothing at all on a machine with no mail
							 client. ===== -->
						<section v-if="supportOpen" class="ob-screen">
							<div class="ob-body">
								<div class="ob-head">
									<h1>Get help with this</h1>
									<p v-if="!supportTicket">
										Tell us what happened and we'll pick it up. We'll attach
										the technical details of this screen automatically, so you
										don't have to describe them.
									</p>
									<p v-else role="status">
										Thanks. We have your request and we'll reply to
										{{ payEmail }}.
									</p>
								</div>
								<div v-if="!supportTicket" class="mx-auto w-full max-w-[560px]">
									<FormControl
										v-model="supportBody"
										type="textarea"
										variant="outline"
										label="What happened?"
										:rows="4"
										placeholder="I tried to pay and..."
									/>
									<details class="mt-3 text-p-xs text-ink-gray-5">
										<summary class="cursor-pointer">
											Details we'll attach
										</summary>
										<pre
											class="mt-1.5 whitespace-pre-wrap break-words rounded-md bg-surface-gray-2 p-2.5"
											>{{ supportContext }}</pre
										>
									</details>
									<!-- Filing failed. Never leave the customer with a dead
										 button: show the address so there is always a way
										 through. -->
									<Banner
										v-if="supportErr"
										class="mt-3"
										type="error"
										:message="`We couldn't file that for you (${supportErr}). Please email ${SUPPORT_EMAIL} and include the details above.`"
									/>
								</div>
								<div
									v-else
									class="mx-auto w-full max-w-[560px] text-center text-p-sm text-ink-gray-5"
								>
									Reference
									<b class="font-medium text-ink-gray-9">{{ supportTicket }}</b
									>. Please don't pay again while we look at it.
								</div>
							</div>
							<div class="ob-foot">
								<button class="ob-back" @click="closeSupport">
									<FeatherIcon
										name="chevron-left"
										class="h-3.5 w-3.5 text-ink-gray-5"
									/>Back
								</button>
								<Button
									v-if="!supportTicket"
									variant="solid"
									:disabled="supportBusy || !supportBody.trim()"
									:loading="supportBusy"
									loading-text="Sending…"
									label="Send to support"
									@click="sendSupport"
								/>
							</div>
						</section>
						<!-- ===== Intro tour (fresh starts only; reconcile routes mid-flight
							 signups straight to the right step, past the tour) ===== -->
						<TourIntro
							v-else-if="state.step === 'intro'"
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
										<!-- GST-exclusive pricing: the headline price above is the base
										     rate, not what gets charged, so this line says so plainly -
										     but only for a plan that actually carries GST (planHasGst).
										     A 0-GST plan, and every plan before get_plans sends
										     gst_percent at all, must not claim "excl. GST" they don't owe.
										     The due-today line right below IS tax-inclusive (it's a real
										     charge amount) - only the base rate needs the caveat. -->
										<div class="text-xs text-ink-gray-5">
											{{ planCycleLabel(p)
											}}<template v-if="planHasGst(p)">
												· excl. GST</template
											>
										</div>
										<!-- #671: what is due TODAY, on the card. The customer
										     used to meet this only on Review, a screen after
										     choosing, so nothing told them the trial starts at
										     zero while they were deciding. -->
										<div
											v-if="planDueToday(p)"
											class="mt-2 text-p-sm font-medium text-ink-gray-8"
										>
											{{ planDueToday(p) }}
										</div>
										<!-- #671: not rendered at all when a plan has no features. The
										     old fallback bullet said "Monthly plan", repeating both the
										     "/mo" on the price and the cycle line directly above it.
										     Dropping that bullet without this guard would leave a
										     featureless plan with an empty list and its margin, a blank
										     gap that reads as a broken card. -->
										<ul
											v-if="planFeatures(p).length"
											class="mt-3.5 grid gap-2"
										>
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
									<div class="flex flex-col gap-1">
										<FormControl
											type="email"
											variant="outline"
											label="Work email"
											:model-value="state.email"
											@update:model-value="
												(v) => {
													state.email = v;
													billing.setIdentity(v, undefined);
													state.identityFromUser = true;
													clearFieldErrorIfValid('email', emailError, v);
												}
											"
											placeholder="you@company.com"
											autocomplete="email"
											required
											aria-required="true"
											:aria-invalid="
												detailsFieldErrors.email ? 'true' : undefined
											"
											:aria-describedby="
												detailsFieldErrors.email
													? 'jv-ob-email-err'
													: undefined
											"
											@blur="touchEmailField"
											@keydown.enter="onDetailsSubmit"
										/>
										<ErrorMessage
											id="jv-ob-email-err"
											:message="detailsFieldErrors.email"
										/>
									</div>
									<div class="flex flex-col gap-1">
										<FormControl
											type="tel"
											variant="outline"
											label="Contact number"
											:model-value="billing.fields.contact.value"
											@update:model-value="
												(v) => {
													billing.setUserValue('contact', v);
													clearFieldErrorIfValid(
														'contact',
														contactError,
														v
													);
												}
											"
											placeholder="+91 98765 43210"
											autocomplete="tel"
											required
											aria-required="true"
											:aria-invalid="
												detailsFieldErrors.contact ? 'true' : undefined
											"
											:aria-describedby="
												detailsFieldErrors.contact
													? 'jv-ob-contact-err'
													: undefined
											"
											@blur="touchContactField"
											@keydown.enter="onDetailsSubmit"
										/>
										<ErrorMessage
											id="jv-ob-contact-err"
											:message="detailsFieldErrors.contact"
										/>
									</div>
									<!-- No separate contact-consent checkbox here (owner decision
										 2026-08-14): consent to be contacted rides the T&C checkbox
										 below (moved onto this step 2026-08-16), and onPayClick still
										 sends contact_consent alongside terms_accepted. Consequence: a
										 lead captured at the Plan step carries no consent until the
										 customer accepts the terms, which now happens on THIS step
										 before Plan is ever reached. -->
									<!-- JvCombo (shared, out of scope) has no declared aria-invalid /
										 aria-describedby / blur props, and does not spread $attrs onto
										 its inner <input>, so those attrs would silently land on its
										 root <div> instead of the actual control. The error message and
										 aria therefore go on THIS wrapper, which we do own, rather than
										 on JvCombo itself. Its error only clears on input (every
										 keystroke already reaches us via update:model-value) - there is
										 no reliable blur signal to hook without editing JvCombo. -->
									<div
										class="col-span-2 flex flex-col gap-1.5"
										:aria-invalid="
											detailsFieldErrors.company ? 'true' : undefined
										"
										:aria-describedby="
											detailsFieldErrors.company
												? 'jv-ob-company-err'
												: undefined
										"
									>
										<!-- #668: hand-rolled because JvCombo has no label prop, so this
										     mirrors FormLabel's required recipe (red asterisk + sr-only
										     text), the same way TriggerDetail.vue does for Autocomplete.
										     A literal "Company *" gave this field a grey asterisk while
										     Work email, a plain FormControl, got FormLabel's red one - the
										     two required fields on one form marked themselves differently.
										     It also read aloud as "Company star" with nothing saying the
										     field was required. -->
										<label for="jv-ob-company" class="text-xs text-ink-gray-5">
											Company
											<span
												class="select-none text-ink-red-3"
												aria-hidden="true"
												>*</span
											>
											<span class="sr-only">(required)</span>
										</label>
										<JvCombo
											id="jv-ob-company"
											:model-value="state.company"
											@update:model-value="
												(v) => {
													state.company = v;
													billing.setIdentity(undefined, v);
													state.identityFromUser = true;
													clearFieldErrorIfValid(
														'company',
														companyError,
														v
													);
												}
											"
											:allow-custom="
												!state.erpnextInstalled ||
												state.companies.length === 0
											"
											aria-required
											autocomplete="organization"
											:options="state.companies"
											placeholder="Acme Inc."
											@enter="onDetailsSubmit"
										/>
										<ErrorMessage
											id="jv-ob-company-err"
											:message="detailsFieldErrors.company"
										/>
									</div>
									<!-- Partner code: optional, closed by default (most customers have
										 none). jarvis only threads the string to admin - no validation
										 here, admin resolves/rejects it. Kept on `state`, not the
										 billing composable, so it is intentionally NOT restored on a
										 mid-onboarding reload (localStorage-persisted like billing) -
										 acceptable for v1 since it's typed once at signup. -->
									<details
										class="col-span-2 text-p-xs text-ink-gray-5"
										:open="state.partnerCodeOpen"
										@toggle="state.partnerCodeOpen = $event.target.open"
									>
										<summary class="cursor-pointer">
											Have a partner code? (optional)
										</summary>
										<div class="mt-2 flex flex-col gap-1">
											<p class="text-p-xs text-ink-gray-5">
												If a Frappe partner referred you, enter their code
												here.
											</p>
											<FormControl
												type="text"
												variant="outline"
												label="Partner code (optional)"
												:model-value="state.partnerCode"
												@update:model-value="
													(v) => (state.partnerCode = v)
												"
												placeholder="PARTNER-CODE"
												@keydown.enter="onDetailsSubmit"
											/>
										</div>
									</details>
									<div
										class="col-span-2 mt-2 text-base font-semibold text-ink-gray-9"
									>
										Billing
									</div>
									<FormControl
										class="col-span-2"
										type="text"
										variant="outline"
										label="Billing address (optional)"
										:model-value="billing.fields.address.value"
										@update:model-value="
											(v) => billing.setUserValue('address', v)
										"
										placeholder="Street, area"
										autocomplete="street-address"
										@keydown.enter="onDetailsSubmit"
									/>
									<FormControl
										type="text"
										variant="outline"
										label="City (optional)"
										:model-value="billing.fields.city.value"
										@update:model-value="
											(v) => billing.setUserValue('city', v)
										"
										placeholder="Chennai"
										autocomplete="address-level2"
										@keydown.enter="onDetailsSubmit"
									/>
									<div class="flex flex-col gap-1">
										<FormControl
											type="text"
											variant="outline"
											label="GSTIN (optional)"
											:model-value="billing.fields.gstin.value"
											@update:model-value="
												(v) => {
													billing.setUserValue('gstin', v);
													clearFieldErrorIfValid('gstin', gstinError, v);
												}
											"
											:placeholder="GSTIN_PLACEHOLDER"
											:aria-invalid="
												detailsFieldErrors.gstin ? 'true' : undefined
											"
											:aria-describedby="
												detailsFieldErrors.gstin
													? 'jv-ob-gstin-err'
													: undefined
											"
											@blur="touchGstinField"
											@keydown.enter="onDetailsSubmit"
										/>
										<ErrorMessage
											id="jv-ob-gstin-err"
											:message="detailsFieldErrors.gstin"
										/>
									</div>
								</div>
								<!-- state.detailsErr stays for genuinely form-wide messages (e.g.
									 onPayClick's "your signup details are missing" guard). Per-field
									 problems render under their own field above instead - a shared
									 bottom banner with no tie to the field is what let a resolved
									 error linger and gave no clue which input it meant. -->
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
								v-if="state.reconnectIntent && !canReconnect"
								class="mx-auto mt-5 max-w-[620px] text-center text-p-sm text-ink-gray-5"
							>
								Enter the email and company this account was registered with, and
								the reconnect option will appear here.
							</p>
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
									Reconnect instead</button
								>. We'll email a code to confirm it's you. Nothing to pay again.
							</p>
							<p
								v-else-if="state.reconnectNeedsCompany"
								class="mx-auto mt-5 max-w-[620px] text-center text-p-sm text-ink-gray-5"
							>
								This email already has a subscription under a different company.
								Enter that company above to reconnect it instead of paying again.
							</p>
							<p
								class="mx-auto mt-3 max-w-[620px] text-center text-p-sm text-ink-gray-5"
							>
								Have a reconnect code from support?
								<button
									class="ob-link"
									:disabled="state.payBusy"
									@click="enterReconnectDirect"
								>
									Enter it here</button
								>.
							</p>
							<!-- Required T&C acceptance (moved here from Review & Pay 2026-08-16
								 so lead-contact consent is captured before the Plan-step lead
								 capture fires). Gates the non-reconnect Continue button below via
								 `:disabled="!state.termsAccepted"`, not the reconnect side-branch
								 above (a returning customer reconnecting an already-paid account
								 never went through this checkbox before, and reconnect makes no
								 signup call that needs terms_accepted / contact_consent). The
								 submit-side check in onDetailsSubmit (see termsError) and
								 payDisabled / onPayClick's own `!state.termsAccepted` guard stay
								 in place as defensive invariants even though the disabled button
								 makes them unreachable via normal click - both are already true
								 by the time Pay is reachable. Checkbox's own `label` prop only
								 takes plain text (no slot, no markup), so the link-bearing
								 sentence is a sibling <label for=...> instead - clicking the
								 embedded <a> navigates without also toggling the box. -->
							<div class="mb-6">
								<div
									class="mx-auto mt-5 flex max-w-[620px] items-start justify-center gap-2"
								>
									<Checkbox
										id="jv-ob-terms"
										:model-value="state.termsAccepted"
										aria-required="true"
										:aria-invalid="
											detailsFieldErrors.terms ? 'true' : undefined
										"
										:aria-describedby="
											detailsFieldErrors.terms
												? 'jv-ob-terms-err'
												: undefined
										"
										@update:model-value="
											(v) => {
												state.termsAccepted = v;
												clearFieldErrorIfValid('terms', termsError, v);
											}
										"
									/>
									<label
										for="jv-ob-terms"
										class="cursor-pointer select-none text-p-sm text-ink-gray-7"
									>
										I agree to the
										<a
											v-if="state.termsUrl"
											:href="state.termsUrl"
											target="_blank"
											rel="noopener"
											class="ob-link"
											@click.stop
											>Terms &amp; Conditions</a
										><span v-else>Terms &amp; Conditions</span>
									</label>
								</div>
								<ErrorMessage
									id="jv-ob-terms-err"
									:message="detailsFieldErrors.terms"
									class="mx-auto mt-1 max-w-[620px] text-center"
								/>
							</div>
							<div class="ob-foot">
								<button class="ob-back" @click="goBack">
									<FeatherIcon
										name="chevron-left"
										class="h-3.5 w-3.5 text-ink-gray-5"
									/>Back to tour
								</button>
								<Button
									v-if="mustReconnect"
									variant="solid"
									label="Reconnect to your workspace"
									:loading="state.detailsSubmitting || state.payBusy"
									@click="startReconnect"
								/>
								<Button
									v-else
									variant="solid"
									label="Continue"
									:loading="state.detailsSubmitting"
									:disabled="!state.termsAccepted"
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
										<h1>
											{{
												paySummaryTrial
													? "Free trial started"
													: "Payment confirmed"
											}}
										</h1>
										<!-- The lead states only what the machine reaching PAID
											 establishes. What is happening to the workspace is left to
											 the phase list below, which reports observations rather
											 than asserting preparation is under way. -->
										<p v-if="!provisioningDelayed" role="status">
											{{
												paySummaryTrial
													? "Auto-pay authorized, and nothing is charged until your trial ends."
													: "Payment received."
											}}
										</p>
										<p v-else role="status">
											Your workspace is taking longer than usual to come
											online. Your payment is complete and nothing more is
											owed.
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
									<!-- jarvis#726: a step-counted progress bar, reading the SAME
										 phase object the row below already renders - see
										 waitPhases.phaseProgress for what it measures and why it
										 goes indeterminate instead of guessing a percentage. Label is
										 just "Step N of 3", not the phase's own sentence - that
										 sentence already renders once, in the row below; repeating it
										 here read as an accidental duplicate rather than emphasis. -->
									<div v-if="!provisioningDelayed" class="ob-progress">
										<StepProgress
											:steps="WAIT_STEPS"
											:current-index="provisioningProgress.current - 1"
											:indeterminate="provisioningProgress.indeterminate"
											:label="`Step ${provisioningProgress.current} of ${provisioningProgress.total}`"
										/>
									</div>
									<!-- Phase columns, side by side under the bar's segments (jarvis
										 wait-phases-horizontal): same three phases as before, laid out
										 as equal-width columns instead of stacked rows so segment one
										 sits above phase one. Column 1 is a settled fact. Column 2
										 renders ONLY what the last provisioning tick observed
										 (waitPhases.js), so "Jarvis is getting ready for you" appears
										 when admin itself answered, and an honest "we couldn't check"
										 appears when it did not. Column 3 names a phase that has not
										 started and says nothing further about it. The active phase's
										 `detail` (waitPhases.js's own provisioningPhase copy) is a full
										 sentence, too long for a column a third of this width, so it
										 renders once, full width, below every column rather than
										 inside one of them. -->
									<!-- ONE live region over the phases AND the detail (round-1 review
										 of the horizontal change). Hoisting the detail out of the
										 active row had given it its own role="status", so a screen
										 reader heard the phase and the sentence explaining that phase
										 as two unrelated announcements. Worse, that region was v-if
										 gated, so it MOUNTED already populated, and a live region
										 announces changes to a region that is already present, not
										 its initial content. Wrapping both means one coherent
										 announcement per tick, and the region outlives the sentence
										 appearing inside it. -->
									<div v-if="!provisioningDelayed" role="status">
										<ul class="ob-phases" role="list">
											<li class="ob-phase ob-phase--done">
												<span class="ob-phase-ico">
													<FeatherIcon name="check" class="h-4 w-4" />
												</span>
												<span class="ob-phase-txt">
													<span class="ob-phase-label"
														>Payment confirmed</span
													>
												</span>
											</li>
											<li
												class="ob-phase"
												:class="`ob-phase--${provisioningStage.state}`"
											>
												<span class="ob-phase-ico">
													<!-- currentColor so the active step matches its
													     sibling step icons (.ob-phase-ico sets the
													     colour), not JvSpinner's brand default. -->
													<JvSpinner
														v-if="provisioningStage.state === 'active'"
														color="currentColor"
														:size="20"
													/>
													<FeatherIcon
														v-else
														name="alert-circle"
														class="h-4 w-4"
													/>
												</span>
												<span class="ob-phase-txt">
													<span class="ob-phase-label">{{
														provisioningStage.label
													}}</span>
												</span>
											</li>
											<li class="ob-phase ob-phase--waiting">
												<span class="ob-phase-ico"
													><i class="ob-phase-dot"></i
												></span>
												<span class="ob-phase-txt">
													<span class="ob-phase-label"
														>Connecting your AI</span
													>
												</span>
											</li>
										</ul>
										<p v-if="provisioningStage.detail" class="ob-phase-detail">
											{{ provisioningStage.detail }}
										</p>
									</div>
									<!-- The sanctioned provisioning illustration (design.md §2.2).
										 min-height, never a percentage height - see SetupNeuralNet's
										 own comment. Dropped at the delayed ceiling, where there is a
										 decision to make and motion would compete with it. -->
									<div
										v-if="!provisioningDelayed"
										class="relative mt-2 min-h-[320px] flex-1"
									>
										<SetupNeuralNet :dark="dark" />
									</div>
									<p
										v-if="!provisioningDelayed"
										class="mx-auto max-w-[420px] text-center text-p-sm text-ink-gray-5"
									>
										Most workspaces are ready within a minute.
									</p>
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
										<h1 ref="recoveryHeading" tabindex="-1">
											Check your email
										</h1>
										<p>
											We sent a confirmation link to <b>{{ payEmail }}</b
											>. Click the link to verify your address, then come
											back here and continue.
										</p>
									</div>
									<p class="text-center text-p-sm text-ink-gray-5" role="status">
										<template v-if="payVerifyExpiry"
											>This link expires on {{ payVerifyExpiry }}. </template
										><template v-else>The link expires in 24 hours. </template
										>Check your spam folder if it doesn't arrive.
									</p>
									<!-- jarvis#297 P0-2a: a truthful confirmation for "Resend the
										 link" - stays up until the customer leaves this screen. -->
									<p
										v-if="resendNote"
										class="mt-1.5 text-center text-p-sm text-ink-gray-5"
										role="status"
									>
										{{ resendNote }}
									</p>
								</div>
								<div class="ob-foot justify-end">
									<div class="flex items-center gap-2">
										<Button
											v-for="a in verifyActions"
											:key="a"
											:variant="payActionVariant(a, verifyActions)"
											:disabled="payActionDisabled(a)"
											:loading="payActionLoading(a)"
											loading-text="Working…"
											:label="payActionLabel(a)"
											@click="onPayAction(a)"
										/>
									</div>
								</div>
							</template>
							<!-- Confirming: the customer paid on the admin-hosted page and came
								 back, and we are asking the control plane whether it landed. Money
								 has left them and no verdict exists yet.
								 jarvis#728: this used to render PaymentConfirmingArt here, but the
								 round trip behind this screen is a single one-shot reconcile
								 (usePaymentFlow's hydrate/reconcileAfterFailure), never a poll -
								 it resolves in well under a second on the common path and hands
								 off to the recovery card on the slow one, so the screen can never
								 reliably reach the "tens of seconds" bar design.md §2.2 sets for
								 using an illustration instead of JvSpinner. A short, honest wait
								 gets the short-wait treatment; a flashed or half-drawn frame is
								 worse than a plain spinner. PaymentConfirmingArt.vue is unchanged
								 and kept for a screen whose wait actually earns it. -->
							<template v-else-if="showConfirming">
								<div class="ob-body ob-body--center">
									<div class="ob-head">
										<h1 role="status">Confirming your payment</h1>
										<p>
											You've paid. We're checking with our payment provider
											that it came through.
										</p>
									</div>
									<div class="mt-2.5 flex justify-center">
										<JvSpinner :size="56" />
									</div>
									<p
										class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
									>
										This usually takes a few seconds. Please don't close this
										page or pay again.
									</p>
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
							<!-- Settling: the customer COMPLETED checkout (pay=done) and the
								 one-shot reconcile came back still-pending - either
								 PAYMENT_CONFIRMATION_PENDING (webhook lag, S.UNKNOWN) or
								 PAYMENT_AUTHORIZED_PENDING_CONFIRM (bank confirming the auto-pay
								 mandate, S.CONFIRM_REQUIRED - the standard trial/auto-pay path).
								 Both are the normal happy path, not a failure. Hold a calm, honest
								 wait while the background auto-poll (runPendingAutoPoll, ~2 min)
								 confirms, instead of dropping straight to the alarming recovery
								 card. Escalates to showRecovery only once the poll gives up
								 (pendingPollStuck) or a real code arrives. Copy is state-keyed
								 (settlingCopy): only the genuinely-authorized state claims
								 "authorized"; the webhook-lag state says "confirming", never
								 "confirmed" - money truth stays server-owned (see paymentCodes.js and
								 the 84s double-mandate incident that shaped that rule). -->
							<template v-else-if="showPaymentSettling">
								<div class="ob-body ob-body--center">
									<div class="ob-head">
										<h1 role="status">{{ settlingCopy.headline }}</h1>
										<p>{{ settlingCopy.body }}</p>
									</div>
									<div class="mt-2.5 flex justify-center">
										<JvSpinner :size="56" />
									</div>
									<p
										class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
									>
										Nothing to do here. Please keep this page open and don't
										pay again.
									</p>
								</div>
							</template>
							<!-- Recovery: unknown / retryable / terminal / confirm-required /
								 reconnect. Coded copy + the two named recovery actions in
								 status-first order. -->
							<template v-else-if="showRecovery">
								<div class="ob-body ob-body--center">
									<div class="ob-head">
										<h1 ref="recoveryHeading" tabindex="-1">
											{{ payCopy.headline }}
										</h1>
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
											:class="
												i < paySummaryRows.length - 1
													? 'border-b border-outline-gray-1'
													: ''
											"
										>
											<span class="text-ink-gray-5">{{ row.label }}</span>
											<b class="font-medium text-ink-gray-9">{{
												row.value
											}}</b>
										</div>
									</div>
									<p
										v-if="pay.supportOffered"
										class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
										role="status"
									>
										Still not resolved after a few checks?
										<button class="ob-link" @click="onPayAction(A.SUPPORT)">
											Contact support</button
										>. We'll place it for you. Please don't pay again.
									</p>
									<!-- X7 (defensive): Reconnect is offered but no identity exists to send
										 it with. Rather than a dead disabled button, ask for the email and
										 company on the existing subscription; typing them fills reconnectIdentity
										 (marked customer-typed, never the site-admin prefill). -->
									<div
										v-if="reconnectNeedsIdentity"
										class="mx-auto mt-4 w-full max-w-[420px]"
									>
										<p class="mb-2 text-center text-p-sm text-ink-gray-5">
											Enter the email and company on your existing
											subscription to reconnect this site.
										</p>
										<div class="flex flex-col gap-2">
											<FormControl
												v-model="state.email"
												type="email"
												label="Email"
												placeholder="you@company.com"
												@update:modelValue="onReconnectIdentityInput"
											/>
											<FormControl
												v-model="state.company"
												type="text"
												label="Company"
												@update:modelValue="onReconnectIdentityInput"
											/>
										</div>
									</div>
									<!-- X8: restart() refused to reset (a payment is still recoverable);
										 say so instead of a silent no-op button. -->
									<p
										v-if="restartHeldNote"
										class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
										role="status"
									>
										{{ restartHeldNote }}
									</p>
									<!-- The outcome of the last "Check payment status". A check that
										 changes nothing must still say so, or the button reads as
										 broken. -->
									<p
										v-if="statusCheckNote"
										class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
										role="status"
									>
										{{ statusCheckNote }}
									</p>
									<!-- The pending-payment auto-poll hit its ceiling with no resolution.
										 Say so plainly instead of quietly giving up - a manual check
										 still works, and support is offered above once the check
										 ceiling was reached. -->
									<p
										v-if="pendingPollStuck"
										class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
										role="status"
									>
										We've been checking automatically for a couple of minutes
										and still don't have an answer. You can check again, or
										contact support and we'll look into it.
									</p>
									<!-- The checkout this signup already has is still open. Say so,
										 because the alternative the customer would otherwise reach for
										 (start a new payment) silently leaves the previous Razorpay
										 subscription live and unreferenced. -->
									<p
										v-if="canResumeCheckout"
										class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
										role="status"
									>
										Your payment page is still open. Continue there to finish
										without starting over.
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
							<!-- plan-09 07-c: rollout flag off → a maintenance hold instead of a
								 fresh checkout. No fallback to any old path. -->
							<template v-else-if="showMaintenanceHold">
								<div class="ob-body ob-body--center">
									<div class="ob-head">
										<h1>Payments are paused right now</h1>
										<p>
											We're doing some scheduled maintenance on secure
											payments. Nothing has been charged. Please check back
											shortly, or contact support if you need to get set up
											today.
										</p>
									</div>
									<Button
										variant="subtle"
										label="Contact support"
										@click="onPayAction(A.SUPPORT)"
									/>
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
													? "Confirm the details below. You'll authorize auto-pay securely. Nothing is charged until your trial ends."
													: "Confirm the details below. You'll complete payment securely."
											}}
										</p>
									</div>
									<!-- A status check can legitimately land the customer back on
										 this card (admin answers "no signup here" when the attempt
										 never created an intent). Silently swapping the screen for a
										 blank form is what made the check button look broken, so the
										 outcome is stated here too. -->
									<Banner
										v-if="statusCheckNote"
										type="info"
										:message="statusCheckNote"
										class="mx-auto mb-4 max-w-[560px]"
										role="status"
									/>
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
											v-for="row in billing.reviewRows.value"
											:key="row.key"
											class="flex items-center justify-between gap-3 border-b border-outline-gray-1 px-4 py-3 text-p-sm"
										>
											<span class="text-ink-gray-5">{{ row.label }}</span
											><b class="font-medium text-ink-gray-9">{{
												row.value
											}}</b>
										</div>
										<div
											v-if="pricing.gstPercent"
											class="flex items-center justify-between gap-3 border-b border-outline-gray-1 px-4 py-3 text-p-sm"
										>
											<span class="text-ink-gray-5">Subtotal</span
											><b class="font-medium text-ink-gray-9">{{
												inrExact(pricing.subtotal)
											}}</b>
										</div>
										<div
											v-if="pricing.gstPercent"
											class="flex items-center justify-between gap-3 border-b border-outline-gray-1 px-4 py-3 text-p-sm"
										>
											<span class="text-ink-gray-5"
												>GST ({{ pricing.gstPercent }}%)</span
											><b class="font-medium text-ink-gray-9">{{
												inrExact(pricing.gstAmount)
											}}</b>
										</div>
										<div
											v-if="pricing.gstPercent"
											class="flex items-center justify-between gap-3 border-b border-outline-gray-1 px-4 py-3 text-p-sm"
										>
											<span class="text-ink-gray-5">Total</span
											><b class="font-medium text-ink-gray-9">{{
												inrExact(pricing.total)
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
									<!-- Plan 01: an Edit affordance under the review rows. Edit returns to
										 Details WITHOUT touching the payment intent; once an intent exists
										 the subsequent Continue persists through the authenticated
										 update_billing facade, never a fresh guest signup. -->
									<div
										v-if="billing.reviewRows.value.length"
										class="mx-auto mt-2 flex max-w-[560px] items-center justify-end gap-3 text-p-xs text-ink-gray-5"
									>
										<button
											class="ob-link shrink-0"
											:disabled="state.payBusy"
											@click="editBilling"
										>
											Edit
										</button>
									</div>
									<!-- Provider discovery in flight (X4): render a loading note so a
										 first load - and especially a Retry, which clears the error to
										 re-probe - never leaves a bare disabled CTA with no explanation. -->
									<div
										v-if="state.providersLoading"
										class="mx-auto mt-3.5 flex max-w-[560px] items-center justify-center gap-1.5 text-center text-xs text-ink-gray-5"
										role="status"
									>
										<JvSpinner :size="14" />
										Checking payment options…
									</div>
									<div
										v-else-if="securedProviderLabel"
										class="mx-auto mt-3.5 flex max-w-[560px] items-center justify-center gap-1.5 text-center text-xs text-ink-gray-5"
									>
										<FeatherIcon name="lock" class="h-3.5 w-3.5" />
										Secured by
										{{ securedProviderLabel }}
									</div>
									<!-- Provider discovery failed (P2-6): never present a gateway
										 the control plane did not confirm. Offer a Retry instead. -->
									<p
										v-else-if="state.providersError"
										class="mx-auto mt-3.5 max-w-[560px] text-center text-xs text-ink-gray-5"
										role="status"
									>
										Payment options are unavailable right now.
										<button class="ob-link" @click="loadPaymentProviders">
											Retry
										</button>
									</p>
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
											We'll email a code to {{ payEmail }}. Enter it to
											connect this site to your existing subscription.
											Nothing to pay again.
										</p>
									</div>
									<!-- #669: how long this link lasts, said while it STILL works.
									     Directly above the pay button because that is where the
									     customer is looking before they step away to fetch a card,
									     and stepping away is the whole scenario: the 45 minute limit
									     used to be disclosed only by the failure message, after the
									     link had already died. -->
									<p
										v-if="payLinkDeadline"
										class="mx-auto mt-3 max-w-[560px] text-center text-p-sm text-ink-gray-5"
									>
										{{ payLinkDeadline }}
									</p>
									<!-- T&C acceptance moved to the Details step (2026-08-16): the
										 checkbox no longer renders here. payDisabled and onPayClick's
										 own `!state.termsAccepted` guard below stay in place as
										 invariants - both are already true by the time Pay is
										 reachable, since Details' Continue is gated on the same flag. -->
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
										:disabled="payDisabled"
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
									<p v-if="state.reconnectDirect">
										Enter the code support gave you and the email your
										subscription is registered under. Connects this site to
										your existing subscription, nothing to pay again.
									</p>
									<p v-else>
										If an account exists for this email, a reconnect code is on
										its way to <b>{{ state.email || "your email" }}</b
										>. It connects this site to your existing subscription,
										nothing to pay again.
									</p>
								</div>
								<FormControl
									v-if="state.reconnectDirect"
									v-model="state.reconnectEmail"
									type="email"
									variant="outline"
									label="Registered email"
									class="mx-auto mb-3 w-full max-w-[320px] text-left"
									placeholder="you@company.com"
									autocomplete="email"
									aria-label="Registered email"
									@keydown.enter="submitReconnectCode"
								/>
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
									<template v-if="state.reconnectDirect">
										Single-use, and expires shortly after support issued it.
									</template>
									<template v-else-if="state.reconnectResentIn > 0">
										You can resend in {{ state.reconnectResentIn }}s.
									</template>
									<template v-else>
										Didn't get it?
										<button class="ob-link" @click="resendReconnectCode">
											Resend code
										</button>
									</template>
								</p>
								<p v-if="!state.reconnectDirect" class="ob-code-note">
									Got a code from support instead?
									<button class="ob-link" @click="enterReconnectDirect">
										Enter it here
									</button>
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
									:disabled="
										!state.reconnectCode.trim() ||
										(state.reconnectDirect && !state.reconnectEmail.trim())
									"
									label="Finish reconnect"
									@click="submitReconnectCode"
								/>
							</div>
						</section>

						<section v-else-if="state.step === 'connect'" class="ob-screen">
							<div class="ob-body">
								<div v-show="state.finishing">
									<!-- Working / finishing: the calm setup screen while the ONE
										 apply operation converges (plan-05 D2). -->
									<template
										v-if="
											!state.connectPhase ||
											state.connectPhase === 'working' ||
											state.connectPhase === 'finishing'
										"
									>
										<div class="ob-head">
											<!-- Follows the live phase (waitPhases.setupHeadline,
												 jarvis#727): this used to be a fixed "Setting up
												 {agentName}" sitting above a phase list that jarvis#722
												 had already made real, so the largest text on the screen
												 was the only part saying nothing. It falls back to
												 exactly that sentence whenever the phase names no
												 subject - a headline is not exempt from jarvis#709. -->
											<h1>{{ setupTitle }}</h1>
											<p>
												{{
													state.finishSubtitle ||
													"We'll take you to chat as soon as your setup is done."
												}}
											</p>
											<!-- Expectation-setting note: this whole template branch IS
												 the working/finishing wait, so the note is on screen for
												 all of it, not just the first frame. Its own line, smaller
												 and greyer than the subtitle above (.ob-head p.ob-head-note
												 out-specifies the shared .ob-head p rule), so it reads as a
												 secondary aside, not a second headline. -->
											<p class="ob-head-note">
												This usually takes 5 to 10 minutes: we're setting
												up a private, isolated workspace dedicated to you.
											</p>
										</div>
										<!-- One smooth progress bar for the whole connect wait
											 (2026-08-16 redesign). The jarvis#726 "Step N of 3" bar
											 plus phase columns stopped working once the jarvis#840
											 checklist made it six columns under a 3-count bar: long
											 wrapping labels and a count that contradicted the list
											 (user report). The 2026-08-14 fix replaced the columns
											 with one SHORT-labeled segment per step, but that just
											 moved the duplication onto the bar itself - six tiny
											 labels sitting above six segments read as a duplicated
											 row of tiles above a bar (user report). The per-step
											 labels are gone; the caption now names the current step
											 ("Step 2 of 6 · Workspace") and the current step still
											 explains itself in one line below (ob-step-explain), so
											 nothing is said twice. Honesty is unchanged: the fill
											 reflects only observed states (connectSteps), and an
											 UNKNOWN current step pulses instead of asserting further
											 progress. -->
										<div class="ob-progress">
											<StepProgress
												:steps="connectSteps"
												:current-index="connectProgress.index"
												:indeterminate="connectProgress.indeterminate"
												:label="connectProgress.caption"
											/>
										</div>
										<!-- ONE live region over the explanation and admin's own
											 detail sentence (jarvis#752/#754), same reason as the
											 provisioning block: separate role="status" regions read
											 as unrelated announcements. -->
										<div role="status">
											<p class="ob-step-explain">
												{{ connectProgress.explain }}
											</p>
											<p
												v-if="readinessStage.detail"
												class="ob-phase-detail"
											>
												{{ readinessStage.detail }}
											</p>
											<!-- The honest usage-limit line (jarvis#840): shown for
											     a beat before chat opens anyway; never a blocker. -->
											<p v-if="preflight.notice" class="ob-phase-detail">
												{{ preflight.notice }}
											</p>
										</div>
										<!-- min-height (not h-full) is load-bearing: SetupNeuralNet's
											 canvas fills via absolute+inset-0, and percentage heights
											 don't resolve against a min-height parent - see its own
											 comment. Don't change this to a fixed h-*. -->
										<div class="relative mt-2 min-h-[320px] flex-1">
											<SetupNeuralNet :dark="dark" />
										</div>
									</template>
									<!-- Admin found this workspace ambiguous and paged a human
										 (authority_repair_required). readiness.js is explicit that the
										 only safe thing this surface can do is show admin's own
										 reassurance: no Retry, no Reconnect, and no illustration,
										 because all three suggest something is in motion when what is
										 actually happening is that a person has to look. -->
									<template v-else-if="state.connectPhase === 'blocked'">
										<div class="ob-head">
											<h1>
												{{
													state.connectTitle ||
													"We couldn't continue setting up"
												}}
											</h1>
											<p v-if="state.connectPaged">
												Something about your workspace needs a person to
												check it, and our team has already been notified.
											</p>
											<p v-else>
												Waiting won't clear this one, so we've stopped
												checking rather than leave you watching a spinner.
											</p>
										</div>
										<div class="mx-auto mt-4 max-w-[560px]">
											<Banner
												v-if="state.connectMessage"
												type="warning"
												:message="state.connectMessage"
											/>
											<p
												v-if="state.connectPaged"
												class="mt-4 text-center text-p-sm text-ink-gray-5"
												role="status"
											>
												There's nothing for you to do here, and nothing
												more to pay. We'll email you as soon as it's
												sorted.
											</p>
											<p
												class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
											>
												Want to talk to someone now?
												<button class="ob-link" @click="openSupport">
													Contact support
												</button>
											</p>
										</div>
									</template>
									<!-- Slice 4b (C10b): the subscription-connect strand that
										 waiting cannot heal. Unlike the paged authority-repair block
										 above, the customer CAN act - so this is a real STOP with a
										 primary Reconnect CTA into the wizard's reconnect entry, and
										 admin's own reason as the body. Never the "bringing your setup
										 online" spinner it used to fall into. -->
									<template v-else-if="state.connectPhase === 'reconnect'">
										<div class="ob-head">
											<h1>
												{{
													state.connectTitle ||
													"Your AI subscription needs reconnecting"
												}}
											</h1>
											<p>
												Waiting won't finish this one. Reconnect your AI
												subscription to pick up where setup left off.
											</p>
										</div>
										<div class="mx-auto mt-4 max-w-[560px]">
											<Banner
												v-if="state.connectMessage"
												type="warning"
												:message="state.connectMessage"
											/>
											<div class="mt-4 flex flex-wrap justify-center gap-2">
												<Button
													variant="solid"
													label="Reconnect"
													@click="reconnectFromStall"
												/>
											</div>
											<p
												class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
											>
												Not sure why this happened?
												<button class="ob-link" @click="openSupport">
													Contact support
												</button>
											</p>
										</div>
									</template>
									<!-- A non-ready terminal (retry / superseded / support) or a
										 deadline timeout: stay HERE with a real recovery action.
										 Never a "continue anyway" jump into a chat that cannot yet
										 answer (review P0-08). -->
									<template v-else>
										<div class="ob-head">
											<!-- Derived (waitPhases.connectHeadline), not hard-coded.
												 Every one of these terminals used to render under
												 "Still finishing setup", which asserts the workspace IS
												 still finishing - the exact claim the message directly
												 below it refuses to make (jarvis#709). -->
											<h1>
												{{
													state.connectTitle ||
													"We couldn't confirm your setup"
												}}
											</h1>
											<p>{{ state.connectMessage }}</p>
										</div>
										<div class="mx-auto mt-4 max-w-[640px]">
											<Banner
												v-if="state.retryAfter > 0"
												type="info"
												:message="`You can retry in ${state.retryAfter}s.`"
											/>
											<div class="mt-4 flex flex-wrap justify-center gap-2">
												<Button
													v-if="state.connectPhase === 'superseded'"
													variant="solid"
													label="Reload and retry"
													@click="reloadConnect"
												/>
												<Button
													v-else
													variant="solid"
													:disabled="state.retryAfter > 0"
													label="Retry"
													@click="retryConnect"
												/>
												<!-- jarvis#727: the way out of a state Retry cannot
													 resolve. Offered ONLY where this attempt watched the
													 pipeline stall or fail on the model the customer chose
													 (see connectModelChangeOffered) - never where nothing
													 was observed, because that would blame a model no one
													 examined. Secondary, not primary: Retry can still be
													 the right answer, it just can no longer be the only
													 answer. -->
												<Button
													v-if="connectModelChangeOffered"
													variant="subtle"
													label="Use a different model"
													@click="chooseDifferentModel"
												/>
												<!-- jarvis#758: a real button, weighted alongside Retry /
													 Use a different model - at the point a customer has hit
													 a wall (and already paid), support is the action most
													 likely to matter and must not read as the least
													 prominent one. `subtle`, matching "Use a different
													 model": design.md 3.1 allows exactly one `solid` button
													 per surface. -->
												<Button
													v-if="state.connectSupportOffered"
													variant="subtle"
													label="Contact support"
													@click="openSupport"
												/>
											</div>
											<!-- jarvis#708: offered the moment a bounded readiness wait
												 runs out with no Ready verdict - never after N retries,
												 same as jarvis_admin_v2#259's checkout-shell poll ceiling.
												 A real exit alongside Retry, not instead of it. The button
												 above is the action; this stays as the reassurance line
												 (jarvis#758 moved the link itself into that button, out of
												 this sentence). -->
											<p
												v-if="state.connectSupportOffered"
												class="mx-auto mt-4 max-w-[420px] text-center text-p-sm text-ink-gray-5"
												role="status"
											>
												Still not resolved? We'll take a look for you.
											</p>
										</div>
									</template>
								</div>
								<div v-show="!state.finishing">
									<div class="ob-head">
										<h1>Connect an AI model</h1>
										<p>
											Choose which AI powers {{ agentName }}, a chat
											subscription or your own API key. You can change this
											anytime in Settings → AI models.
										</p>
									</div>
									<div class="mx-auto max-w-[640px]">
										<LlmPoolEditor
											ref="poolRef"
											:editable="true"
											:modes="['quick']"
											:footerless="true"
											:host-busy="savingConnect"
											@ready="connectReady = $event"
											@subscription-testing="subscriptionTesting = $event"
										/>
										<!-- Why the last Start was refused: a rejected apply verdict
											 (error) or the start gate (no passing probe / no account). -->
										<Banner
											v-if="state.connectBlockReason"
											class="mt-3"
											:type="
												state.connectPhase === 'rejected'
													? 'error'
													: 'warning'
											"
											:message="state.connectBlockReason"
										/>
									</div>
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
									 so the step never shows without a primary action. Also disabled
									 while the editor's own subscription Test is running (see
									 subscriptionTesting above) - the two must never push the same
									 desired pool at once. -->
								<Button
									variant="solid"
									:disabled="
										!connectReady || savingConnect || subscriptionTesting
									"
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
import { useRouter } from "vue-router";
import { Button, FormControl, FeatherIcon, ErrorMessage, Checkbox } from "frappe-ui";
import { useJarvisTheme } from "@/theme";
import LlmPoolEditor from "@/components/LlmPoolEditor.vue";
import JvCombo from "@/components/JvCombo.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import JarvisMark from "@/components/JarvisMark.vue";
import Banner from "@/components/Banner.vue";
import StepProgress from "@/components/StepProgress.vue";
import TourIntro from "@/onboarding/TourIntro.vue";
import SetupNeuralNet from "@/onboarding/SetupNeuralNet.vue";
import cashfreeLogo from "@/assets/cashfree.png";
import {
	STEPS_MANAGED,
	nextStep,
	prevStep,
	planDueToday,
	planPricing,
	planSubtitleFor,
} from "@/onboarding/steps";
import { inrExact, planAmount, planSuffix, planHasGst } from "@/account/format";
import {
	isReadyForChat,
	runChatPreflight,
	getLlmApplyOperation,
	listPlans,
	listPaymentProviders,
	reconnectAvailable,
	startAccountReconnect,
	checkAccountReconnect,
	redeemReconnectCode,
	getAccountDefaults,
	getCompanyOnboardingDefaults,
	updateBilling,
	onboardingPaymentApi,
	supportCreateTicket,
	captureOnboardingLead,
	getTermsUrl,
} from "@/api";
import {
	createOperationController,
	classifyOperation,
	operationStore,
	OP_PHASE,
} from "@/lib/llmOperation.js";
import { readinessWaitExhaustedMessage } from "@/onboarding/readinessWait.js";
import {
	PHASE_KIND,
	provisioningPhase,
	readinessPhase,
	inFlightPhase,
	setupHeadline,
	connectHeadline,
	phaseProgress,
} from "@/onboarding/waitPhases.js";
import { forgetReady, hasReconnectIntent, landingStep } from "@/onboarding/readiness.js";
import { errMessage as errMsg } from "@/lib/errors";
import { report as reportError } from "@/lib/errorReporter";
import { agentName } from "@/branding";
import { createPaymentFlow } from "@/onboarding/usePaymentFlow";
import {
	STATES as PAY_STATES,
	canNavigateToPay,
	remainingCooldownSeconds,
	remainingResendCooldownSeconds,
	isTerminalForPayment,
} from "@/onboarding/paymentMachine";
import {
	ACTIONS,
	ACTION_LABELS,
	CODES,
	TONE,
	actionLabelFor,
	copyFor,
	payLinkDeadlineNote,
} from "@/onboarding/paymentCodes";
import { CHECKOUT_NAV_KEY, shouldHonorCheckoutReturn } from "@/onboarding/checkoutNav";
import { makeTelemetryReporter } from "@/onboarding/paymentTelemetry";
import { readCookie } from "@/lib/user";
import { useBillingDetails, billingEditAction } from "@/onboarding/useBillingDetails";
import { gstinError, GSTIN_PLACEHOLDER } from "@/onboarding/gstin";
import { isExpectedCompanyDefaultsMiss } from "@/onboarding/companyDefaultsMiss";

const router = useRouter();
const { effectiveDark: dark, paletteVars } = useJarvisTheme();

// The 4 named wizard steps shown on the rail. The intro tour is chromeless
// (no rail entry).
const RAIL = [
	{ id: "details", label: "Details" },
	{ id: "plan", label: "Plan" },
	{ id: "pay", label: "Pay" },
	{ id: "connect", label: "Connect" },
];

// Unlabelled StepProgress segments for the PROVISIONING wait bar, which counts
// steps (waitPhases.phaseProgress) without naming them the way RAIL does. The
// connect wait used to share this constant; since the 2026-08-14 redesign it
// has its own labeled six-step array (connectSteps), which is exactly the
// "give it its own named constant" escape hatch the #763 round-1 review
// prescribed for a bar that needs a different length - a computed there is
// justified because its states change per tick, unlike this static shape.
const WAIT_STEPS = Object.freeze(Array.from({ length: 3 }, (_, i) => Object.freeze({ id: i })));

// Frame subtitle next to the brand mark, mirroring the active step's title.
const FRAME_SUBS = {
	intro: "Meet Your AI Teammate",
	plan: "Choose your plan",
	details: "Your details",
	pay: "Review & pay",
	reconnect: "Reconnect your subscription",
	connect: "Connect an AI model",
};

// ---- step machine -----------------------------------------------------------
// `state.step` walks STEPS_MANAGED (intro → plan → details → pay → connect).
const state = reactive({
	step: "intro",
	// details (Your Details step)
	email: "",
	company: "",
	companies: [],
	// Whether ERPNext is installed on the site (from getAccountDefaults). Gates
	// whether the Company field below is a constrained picker or free text --
	// see the allow-custom binding on the JvCombo further down.
	erpnextInstalled: false,
	detailsErr: "",
	// Optional partner-code passthrough (top-level kwarg to admin, NOT part of
	// `billing`). Deliberately kept here rather than on the `billing` composable:
	// billing is localStorage-persisted so a mid-onboarding reload restores it,
	// but a partner code is entered once at signup and does not need that -
	// leaving it off the persisted composable means it is simply NOT restored
	// on reload, which is acceptable for v1.
	partnerCode: "",
	// Whether the partner-code disclosure (collapsed by default, see the
	// `<details>` in the Details step template) is open. Forced open when a
	// details-rejection walk-back names the partner code, so the field the
	// customer must fix is not hidden inside a closed disclosure.
	partnerCodeOpen: false,
	// The four billing inputs (contact phone, address, city, GSTIN) live in the
	// `billing` composable below (provenance-aware, fenced, namespaced) — Plan 01.
	// True once a payment intent exists (a signup call created checkout handles,
	// or reconcile found a live mid-flight order): billing edits then save through
	// the authenticated update_billing facade, never a fresh guest signup.
	intentExists: false,
	// True while the customer is editing billing after returning from Review & Pay
	// (the card's "Edit" button), so onDetailsSubmit returns to Pay instead of
	// walking forward through Plan.
	billingEditReturn: false,
	// True once the customer THEMSELVES typed the email/company (not prefilled
	// from getAccountDefaults). Recovery/reconnect identity must never fall back to
	// prefill - a resumed page's prefill can be the SITE ADMIN's email, not the
	// payer's (plan 02 P1-7 / C02-3). The fresh flow may use what the user typed;
	// recovery uses server truth or an honest placeholder, never this unless the
	// user set it.
	identityFromUser: false,
	// Disables the forward action while onDetailsSubmit awaits the eligibility resolve (no double-submit).
	detailsSubmitting: false,
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
	// Arrived from the chat banner: the fields are prefilled from the SITE, which
	// need not be the account, so say what to type when the offer stays hidden.
	reconnectIntent: false,
	reconnectEligible: false,
	reconnectNeedsCompany: false,
	reconnectCode: "",
	reconnectResentIn: 0,
	// Direct (operator-issued) reconnect: the customer got a code from support out
	// of band, so there is NO customer-started request to poll. They redeem the code
	// PLUS their registered email (the second factor) in one shot. reconnectDirect
	// switches the code screen and submit path; reconnectEmail is the typed factor.
	reconnectDirect: false,
	reconnectEmail: "",
	paymentProvider: "", // gateway chosen on Review & Pay: "razorpay" | "cashfree"
	// Required Details-step checkbox (T&C + lead-capture contract; moved off
	// Review & Pay 2026-08-16 so consent is captured before the Plan-step lead
	// capture fires). NOT localStorage-persisted - a legal acceptance is
	// re-asked every time the customer lands fresh on this screen, same as it
	// would be on any checkout. Details' Continue stays blocked until this is
	// true, and only a literal true is ever sent to start_signup. payDisabled
	// and onPayClick's own guard keep checking it too (invariants that are
	// always true by the time Pay is reachable). Accepting it also grants
	// contact consent (owner decision 2026-08-14): the terms cover being
	// contacted about the account.
	termsAccepted: false,
	// The public /terms URL (marketing site, or a rebranded admin's own
	// /terms), via jarvis.onboarding.get_terms_url, fetched best-effort on
	// mount so it is already available by the time the Details step (the
	// second screen) renders. Empty only if that API call itself fails -
	// the checkbox label then renders plain unlinked text instead of a
	// dead link.
	termsUrl: "",
	// Gateways the operator has actually enabled, narrowed to what this build can
	// render. Starts EMPTY and stays empty on a discovery failure (plan 02 P2-6):
	// seeding "razorpay" faked a choice the control plane never confirmed, so a
	// bench whose default is Cashfree (or where Razorpay is disabled) would have
	// presented - and submitted - a processor the server then rejects. An empty
	// list degrades to a clear unavailable/retry state instead of inventing one.
	availableProviders: [],
	providersLoading: false,
	providersError: false,
	// plan-09 07-c rollout flag (site-level boolean, default ON). The admin-hosted
	// checkout is the ONLY payment path after cutover, so this does NOT toggle
	// old-vs-new behaviour - it gates ROLLOUT MESSAGING: when the operator disables
	// it, the pay step shows a maintenance hold instead of taking the customer to
	// checkout. Defaults ON (an older backend that omits the key stays enabled).
	paymentUiV2: true,
	// payErr / payBusy drive the reconnect-code step's own error + button state
	// (the payment machine owns everything on the Pay step).
	payErr: "",
	payBusy: false,
	// True when reconcile landed us directly on "connect" (signup + payment
	// completed in an earlier session): there is no local plan/email/company
	// context, so Back to Review & Pay is hidden (it would re-run start_signup
	// with empty args).
	reconciledConnect: false,
	// Connect (Start chatting) is driven by ONE durable apply-operation controller
	// (plan-05 D2). `finishing` covers the editor with the setup screen; connectPhase
	// is the controller-projected UI phase ("" = editable form; working/finishing =
	// spinner; retry/superseded/support = a recovery panel with a real action; a
	// REJECTED verdict returns to the editable form with connectBlockReason set).
	// finishSubtitle is the spinner's state-derived line. retryAfter counts down a
	// per-operation rate-limit cooldown; connectBlockReason is the inline reason the
	// gate refused (no probe / no account) when the form is still shown.
	// connectSupportOffered (jarvis#708) is true once a bounded chat-readiness wait
	// (waitForChatReadiness / followLegacyReadiness) has run out at least once THIS
	// attempt with no Ready verdict - the same "hand off to a person at the existing
	// poll ceiling" moment jarvis_admin_v2#259 uses for the checkout shell's own
	// confirm poll. Reset only in saveConnect (a genuinely new attempt); a Retry that
	// re-follows the SAME stuck operation must not silently withdraw the offer.
	finishing: false,
	finishSubtitle: "",
	connectPhase: "",
	connectMessage: "",
	// The recovery panel's own heading. Derived from the same place as
	// connectMessage rather than hard-coded in the template, where a single
	// "Still finishing setup" sat above all of them and claimed progress the
	// message underneath deliberately refuses to claim (jarvis#709).
	connectTitle: "",
	// True only when the STOP came from a verdict where support was already
	// notified (authority_repair_required). A paused subscription or a moved
	// account also stop the wait, but nobody was paged and the customer has to
	// act, so they must not get the "our team is on it" reassurance.
	connectPaged: false,
	connectBlockReason: "",
	connectSupportOffered: false,
	retryAfter: 0,
});

// What the LAST readiness poll observed, projected into the setup screen's
// phase. NULL means no poll has reported yet - which is NOT the same as a poll
// that answered nothing. Seeding it as `answered: false` made the row render
// "We couldn't reach your workspace to check" on the first frame after a
// successful save, announcing a failed check before one had been attempted.
const readinessSeen = ref(null);
// jarvis#752: the apply-operation poll's OWN chat_readiness_reason, mirrored from
// the last onOpUpdate tick. This is a DIFFERENT source than readinessSeen above -
// the operation carries admin's free-text explanation from the moment a save is
// accepted (well before the readiness wait that seeds readinessSeen ever starts),
// so a route that cannot serve (an exhausted subscription, an unverified account)
// is nameable during the ordinary "Applying"/"Finishing" phase instead of only
// after a five-minute deadline. Mirrors the CURRENT tick exactly - see onOpUpdate -
// so a tick that named nothing blanks it rather than sticking an earlier one.
const opReadinessDetail = ref("");
const readinessStage = computed(() =>
	readinessSeen.value
		? readinessPhase(readinessSeen.value)
		: // The apply operation is what is running, and the operation controller
		  // reported that, so naming it is grounded.
		  inFlightPhase(
				"Applying your AI connection",
				PHASE_KIND.LLM_APPLY,
				opReadinessDetail.value
		  )
);
// The connect wait's bar is connectSteps/connectProgress (defined with the
// jarvis#840 preflight state they read, further down) - it stopped using
// waitPhases.phaseProgress in the 2026-08-14 redesign because its six steps
// are named, while phaseProgress only ever counts an unnamed 3.

// Plan 01 billing state. Namespaced by site identity + logged-in user so one
// site's / user's transitional billing PII can never prefill another's on a
// shared browser (P0-02/C01-6). Owns provenance, the stale-response fence, the
// ack-gated storage promise, and the Review & Pay ↔ payload single source.
const billing = useBillingDetails({
	site: (typeof window !== "undefined" && window.location && window.location.host) || "",
	user: readCookie("user_id") || "",
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
// Forced-reconnect gate: an eligible returning (email, company) may ONLY reconnect.
// Keyed on reconnectIdentity (what the customer typed), never the admin prefill.
const mustReconnect = computed(
	() =>
		canReconnect.value && !!reconnectIdentity.value.email && !!reconnectIdentity.value.company
);
// Last (email, company) a request was issued for, so cancel->Continue reuses it (admin caps 5/hr). Not cleared on cancel.
let reconnectIssued = null;

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
// Lead capture (lead-capture + T&C frozen contract): fire-and-forget upsert of
// a Jarvis Lead on entering the Plan step, so an abandoned onboarding still
// leaves something for outreach. `immediate: true` covers a RESUME that lands
// directly on "plan" (readiness.js's landingStep) as well as a genuine forward
// transition from Details. Only fires once email+company are both present -
// admin's own guard treats a missing/invalid email as a no-op anyway, and
// firing before either exists would just upsert an unidentifiable row. Never
// awaited and never lets a rejection escape the watcher: a burst re-entry
// (Back then Continue again) simply re-upserts and bumps last_seen, which is
// the intended behaviour, not a bug to dedupe.
watch(
	() => state.step,
	(step) => {
		if (step !== "plan") return;
		if (!(state.email || "").trim() || !(state.company || "").trim()) return;
		try {
			captureOnboardingLead({
				email: state.email,
				company: state.company,
				billing: billing.buildBilling(),
				plan: state.planName || "",
				step: "plan",
				// No contact_consent here (defaults false upstream): the Details-step
				// consent checkbox was folded into the T&C acceptance (owner decision
				// 2026-08-14), which only happens later at Review & Pay. An abandoned
				// lead therefore carries no consent to contact.
				partner_code: state.partnerCode?.trim() || undefined,
				site_origin:
					(typeof window !== "undefined" && window.location && window.location.origin) ||
					"",
			}).catch(() => {});
		} catch (e) {
			/* fire-and-forget: must never break the step transition */
		}
	},
	{ immediate: true }
);
// Plan 01: a Company change re-fetches ERP-derived billing defaults (debounced +
// fenced inside fetchCompanyDefaults). Fires on the prefilled default Company too
// (harmless: it only fills empty/erp_default fields, never user edits). immediate
// is off so it doesn't race prefillAccount/restore on mount — onMounted kicks the
// first fetch explicitly after both have run.
watch(
	() => state.company,
	() => scheduleCompanyDefaults()
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
// names it. A trial still authorizes a mandate now (the free-plan "no payment
// at all" model was removed), so the gateway chooser must show on trial plans
// too.
// The only gateways this wizard can render a chip for and hand a sheet to. The
// chooser template hard-codes a Razorpay and a Cashfree chip and the checkout
// dispatcher knows only those two families, so this is the closed set the
// provider discovery is narrowed against (D10) - never a value the SPA cannot open.
const KNOWN_PROVIDERS = ["razorpay", "cashfree"];
// The customer-facing gateway name, or "" for anything not in the known set - so
// the "Secured by …" line fails CLOSED (renders nothing) rather than defaulting to
// "Razorpay" for a provider that is not Razorpay (D10).
const providerLabel = (p) => (p === "razorpay" ? "Razorpay" : p === "cashfree" ? "Cashfree" : "");
const securedProviderLabel = computed(() => providerLabel(state.paymentProvider));

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
const showProviderChooser = computed(() => providerChoices.value.length >= 1);
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
// Fail-CLOSED (plan 02 P2-6): if discovery fails or returns nothing, the wizard
// degrades to a clear unavailable/retry state and NEVER invents a provider - a
// gateway the server would reject must not be presented or submitted. It is not
// expected to charge (the server refuses it), but it is a poor recovery, so we
// surface a Retry instead.
async function loadPaymentProviders() {
	state.providersLoading = true;
	state.providersError = false;
	try {
		const r = (await listPaymentProviders()) || {};
		// plan-09 07-c: the rollout flag. Absent on an older backend → default ON.
		state.paymentUiV2 = r.payment_ui_v2 !== false;
		// Narrow to the gateways this wizard can actually render and open (D10). The
		// chooser draws a Razorpay/Cashfree chip and the checkout dispatcher only
		// knows those two families, so a third gateway the control plane names would
		// otherwise pass `.filter(Boolean)`, enable the CTA, render an EMPTY chooser
		// box (no chip matches), and mislabel the "Secured by …" line - then Pay would
		// post a provider the SPA cannot open. Fail closed instead: an unknown-only
		// answer narrows to nothing and takes the unavailable/Retry path below.
		const providers = (Array.isArray(r.providers) ? r.providers : []).filter((p) =>
			KNOWN_PROVIDERS.includes(p)
		);
		if (!providers.length) {
			state.availableProviders = [];
			state.paymentProvider = "";
			state.providersError = true;
			return;
		}
		state.availableProviders = providers;
		// Preselect admin's default; never leave the selection pointing at a
		// gateway that is no longer offered, or Pay would post a provider the
		// server refuses.
		const preferred = providers.includes(r.default) ? r.default : providers[0];
		if (!providers.includes(state.paymentProvider)) state.paymentProvider = preferred;
	} catch (e) {
		state.availableProviders = [];
		state.paymentProvider = "";
		state.providersError = true;
	} finally {
		state.providersLoading = false;
	}
}

// Pay CTA copy (plan 02 P2-2). The first click starts the signup and may need
// email verification BEFORE any gateway opens, so it must not promise an
// immediate charge ("Pay ₹X" overstated it): a paid plan reads "Continue to
// payment"; an autopay trial keeps its trial wording (C02-4), since that click
// authorizes the auto-pay mandate rather than charging today.
const payCta = computed(() => {
	if (isTrialPlan.value) return "Start free trial";
	return "Continue to payment";
});
// A gateway must be confirmed by discovery before the first click (P2-6). Both a
// paid plan and an autopay trial need one (the trial authorizes a mandate), so
// the CTA is disabled until then and the unavailable/retry note stands in for it.
const payProviderReady = computed(() => !!state.paymentProvider);
// The CTA's full gate, named rather than left as three booleans inline in the
// template: busy, no confirmed gateway yet, or the required Terms & Conditions
// box unticked. The third leg is an invariant now, not a live gate - the
// checkbox itself lives on Details and blocks Continue there, so by the time
// Pay renders state.termsAccepted is already true. Kept here anyway (per the
// T&C + lead-capture contract) so a future change to Details' gate fails
// closed instead of silently reopening this hole.
// payBusyView is declared further down; safe to close over here since this
// getter only runs at render, after the whole setup script has executed.
const payDisabled = computed(
	() => payBusyView.value || !payProviderReady.value || !state.termsAccepted
);
// #669: how long the checkout link is good for. Reads the machine's own
// payTokenExpiresInS, which is cleared with the token it belongs to, so this
// renders nothing the moment the link stops being openable rather than leaving a
// reassuring countdown over a dead link. Empty string when there is no honest
// number to give, and the v-if means no empty paragraph is left behind.
const payLinkDeadline = computed(() => payLinkDeadlineNote(pay.value.payTokenExpiresInS));

// Review-card labels (preview .rev): "Pro · Monthly" plan row and a plain
// amount in the emphasized total row.
const planRowLabel = computed(() => {
	const p = selectedPlan.value;
	if (!p.plan_name) return "";
	return p.billing_cycle ? `${p.plan_name} · ${p.billing_cycle}` : p.plan_name;
});
// #671: planDueToday lives in onboarding/steps.js so the Plan cards and this Review
// row read ONE definition, and so it is unit-testable under node --test.
const dueTodayLabel = computed(() => planDueToday(selectedPlan.value));
// GST tax breakdown (2026-08): base/GST/total split for the Review card's
// Subtotal/GST/Total rows. Same canonical formula as planDueToday's `total` -
// planPricing is the one place that math lives.
const pricing = computed(() => planPricing(selectedPlan.value));

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
// A PAID customer whose workspace is not chat-ready yet, for a reason that means
// "the AI model still has to be chosen". Every one of these resumes on Connect,
// which is the step the customer expects to land on the moment their payment goes
// through.
//
// This used to be an inline two-value check (llm_credentials, llm_pool_provisioning)
// while readiness.js's own NOT_ONBOARDED_REASONS listed five. The two that were
// missing are the ones a fresh signup actually hits: `llm_provisioning` is the
// direct/single-model analogue of llm_pool_provisioning, and `llm_setup` is the
// half-finished signup whose payment has only just landed. Both fell through to
// the else-branch below and parked the customer on the Pay step's provisioning
// spinner instead of taking them to the AI model step, which is the "after payment
// it did not go to the AI model page" report.
const PAID_NEEDS_CONNECT_REASONS = new Set([
	"llm_credentials",
	"llm_pool_provisioning",
	"llm_provisioning",
	"llm_setup",
]);

// Returns the passive {paid, truthKnown, notStarted} truth flow.hydrate() read
// (or null on a fail-open), so the caller (onMounted's fresh-mount return heal)
// can tell whether a mid-flight signup actually exists without re-deriving it
// from state.step.
async function reconcileMidFlightSignup() {
	let truth;
	try {
		truth = await flow.hydrate(); // {paid: true|false|null, truthKnown, notStarted}
	} catch (e) {
		return null; // fail open to the intro tour
	}
	// Day one: no signup on this site. Leave the default intro tour (fresh start).
	if (truth.notStarted) return truth;

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
		if (ready && PAID_NEEDS_CONNECT_REASONS.has(ready.reason)) {
			state.reconciledConnect = true;
			state.step = "connect";
			return truth;
		}
		// Paid but not yet chat-ready (container provisioning): land on Pay, where
		// the machine renders the paid receipt + "preparing your workspace". A paid
		// answer proves an intent exists (Plan 01), so later billing edits route
		// through the authenticated update_billing facade, never a fresh guest signup.
		state.intentExists = true;
		state.step = "pay";
		return truth;
	}

	// Not paid, or payment truth could not be established. Either way the connect
	// shortcut must NOT fire - "has credentials" is not "has paid". If hydrate()
	// absorbed a real payment state (verification / unknown / failed /
	// confirm_required / reconnect), render it on the Pay step; the reconnect
	// state shows its offer there too. A customer who has genuinely not started
	// anything leaves the machine on REVIEW and stays on the intro tour.
	if (pay.value.value !== S.REVIEW) {
		// A live non-REVIEW machine state means a signup/payment intent exists, so
		// billing edits route through update_billing, not a guest signup (Plan 01).
		state.intentExists = true;
		state.step = "pay";
	}
	return truth;
}

// ---- Plan (Choose Your Plan) ------------------------------------------------
async function loadPlans() {
	state.plansErr = "";
	state.plansLoading = true;
	try {
		state.plans = (await listPlans()) || [];
		// #671: a single-choice question must not need a click to answer it. With
		// exactly one plan on offer the card started unselected and Continue was
		// disabled until you clicked the only thing on screen, which is a dead click.
		//
		// Only ever fills an EMPTY selection, so a customer who came Back to this step
		// keeps whatever they picked, and a real multi-plan catalog is untouched: with
		// two or more plans the choice is genuine and preselecting one would be us
		// answering it for them.
		if (state.plans.length === 1 && !state.planName) {
			state.planName = state.plans[0].name;
		}
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
// Email + Company + Contact number are required; GSTIN is validated (gstin.js)
// but optional - the four billing inputs are provenance-aware state owned by the `billing`
// composable (Plan 01): edits are user-owned, the transitional localStorage
// snapshot is namespaced by site+user and cleared only after admin's
// billing_saved ack, and a Company change fetches ERP-derived defaults behind
// a stale-response fence (fetchCompanyDefaults below).

// Per-field validators: each returns "" for a valid (or, where the field is
// optional, blank) value, else the exact sentence shown under that field. Kept
// as pure functions of a raw value so onDetailsSubmit's "validate everything
// at once" and the per-field blur/input handlers below share one source of
// truth instead of drifting. Email distinguishes EMPTY from MALFORMED - an
// empty form used to report "Enter a valid email address", which is simply
// untrue of a field nobody has typed into yet.
function emailError(value) {
	const v = (value || "").trim();
	if (!v) return "Work email is required.";
	return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v) ? "" : "Enter a valid email address.";
}
function companyError(value) {
	return (value || "").trim() ? "" : "Company name is required.";
}
// Contact number is mandatory (non-empty after trim) but not format-checked -
// see the onboarding brief: no phone-number regex, admin's own normalizer is
// the source of truth for shape.
function contactError(value) {
	return (value || "").trim() ? "" : "Enter a contact number.";
}
// gstinError (gstin.js) already treats a blank value as "" (GSTIN is optional).
// Required T&C checkbox (moved here from Review & Pay 2026-08-16): unlike the
// other Details fields this isn't touched on blur (a checkbox has none worth
// hooking). The non-reconnect Continue button is now `:disabled` until this is
// true, so this error string is normally unreachable by click; the check still
// runs in onDetailsSubmit as a defensive invariant (e.g. a keyboard/programmatic
// submit that bypasses the disabled button), and is cleared live by
// clearFieldErrorIfValid as soon as the box is ticked.
function termsError(value) {
	return value ? "" : "Please accept the Terms & Conditions to continue.";
}

// Details-step field errors, one bucket per field so each renders under its
// OWN input via ErrorMessage instead of a single shared bar at the bottom with
// no tie to what's wrong. Set all at once by onDetailsSubmit (every failure
// shown together, not one submit per error); state.detailsErr stays reserved
// for genuinely form-wide messages (see onPayClick's missing-details guard).
const detailsFieldErrors = reactive({ email: "", company: "", contact: "", gstin: "", terms: "" });
function touchEmailField() {
	detailsFieldErrors.email = emailError(state.email);
}
function touchCompanyField() {
	detailsFieldErrors.company = companyError(state.company);
}
function touchContactField() {
	detailsFieldErrors.contact = contactError(billing.fields.contact.value);
}
function touchGstinField() {
	detailsFieldErrors.gstin = gstinError(billing.fields.gstin.value);
}
// Called on every keystroke: only ever CLEARS an error that no longer applies
// - it never shows a NEW one while the customer is still typing. Full
// (re)validation happens on blur (touch*Field above) and on submit.
function clearFieldErrorIfValid(name, errorFn, value) {
	if (detailsFieldErrors[name] && !errorFn(value)) detailsFieldErrors[name] = "";
}

// ERP-derived billing defaults for the selected Company. Debounced (the Company
// combo emits on every keystroke), fenced (beginCompanyFetch mints a monotonic
// generation; applyDefaults drops anything that isn't the newest request for the
// still-selected Company), and fail-closed (any error just leaves the fields as
// beginCompanyFetch left them — prior-Company ERP values cleared, user edits kept).
// Telemetry is presence-only: a stable code, never a billing value.
let companyDefaultsTimer = null;
function scheduleCompanyDefaults() {
	if (companyDefaultsTimer) clearTimeout(companyDefaultsTimer);
	companyDefaultsTimer = setTimeout(fetchCompanyDefaults, 300);
}
async function fetchCompanyDefaults() {
	const company = (state.company || "").trim();
	// beginCompanyFetch clears prior-Company erp_default values (and mints the
	// generation) whether or not a network call follows, so a switch to a custom
	// Company that resolves nothing still drops the previous Company's ERP data.
	const gen = billing.beginCompanyFetch(company);
	if (!company) return;
	try {
		const resp = await getCompanyOnboardingDefaults(company);
		if (resp && resp.ok) billing.applyDefaults(resp, gen, company);
	} catch (e) {
		// COMPANY_DEFAULTS_FORBIDDEN / _NOT_FOUND surface as a 4xx (thrown here);
		// nothing to apply. _NOT_FOUND is EXPECTED noise - this fires on every
		// keystroke of a Company that hasn't resolved yet (debounced, but still
		// constant), so it is never reported; genuinely unexpected failures
		// (FORBIDDEN, network, 5xx) still are. Presence-only report, no PII.
		if (!isExpectedCompanyDefaultsMiss(e)) {
			reportError({ surface: "onboarding", error_code: "company_defaults", message: "" });
		}
	}
}

async function onDetailsSubmit() {
	// One submit at a time (the eligibility resolve below is awaited).
	if (state.detailsSubmitting) return;
	state.detailsErr = "";
	state.email = (state.email || "").trim();
	state.company = (state.company || "").trim();
	// Validate every field AT ONCE, not one submit per error: each failure lands
	// under its own field via detailsFieldErrors, so a customer sees everything
	// wrong in one pass instead of fixing email only to have the next click
	// reveal Company was empty too. A bad GSTIN blocks here too, on this same
	// step, instead of dead-ending at the pay button three screens later.
	// terms is the deliberate exception: it's checked separately, further down,
	// AFTER the reconnect branch resolves - batching it in here would gate the
	// reconnect branch too (item 5), which it must not. A customer with both a
	// bad GSTIN and an unticked box sees the GSTIN error first and the terms
	// error only on a second submit; accepted as the cost of that exemption.
	touchEmailField();
	touchCompanyField();
	touchContactField();
	touchGstinField();
	if (
		detailsFieldErrors.email ||
		detailsFieldErrors.company ||
		detailsFieldErrors.contact ||
		detailsFieldErrors.gstin
	)
		return;
	billing.persist();
	// Editing billing after Review & Pay: return straight to Pay, and — once an
	// intent exists — save the edit through the authenticated update_billing
	// facade (NEVER a fresh guest signup, which would create/replace the intent).
	if (state.billingEditReturn) {
		state.billingEditReturn = false;
		saveBillingEdit();
		state.step = "pay";
		return;
	}
	// Entering Review & Pay fresh from Details: the payment machine owns the pay
	// sub-state now, and a fresh review renders from its REVIEW state. Clear the
	// reconnect-code step's own error surface so a stale one does not linger.
	state.payErr = "";
	statusCheckNote.value = "";
	// Forced-reconnect gate: an eligible existing (email, company) the customer ASSERTED
	// may only reconnect. Fresh resolve here (cancelling the debounce) closes the
	// type-then-Continue race; fails closed (guest signup() 409 still blocks a duplicate).
	state.detailsSubmitting = true;
	try {
		clearTimeout(eligibilityTimer);
		await refreshReconnectEligibility();
		const id = reconnectIdentity.value;
		if (state.reconnectEligible && id.email && id.company) {
			await startReconnect();
			return;
		}
	} finally {
		state.detailsSubmitting = false;
	}
	// Required T&C acceptance: checked here, AFTER the reconnect branch above
	// (which already returned if it applied), so this gate never blocks a
	// returning customer reconnecting an existing paid account - reconnect
	// makes no signup call and never needed terms_accepted/contact_consent.
	// Only the path that actually advances to Plan is gated. The Continue
	// button is `:disabled` until termsAccepted is true, so in normal use this
	// is already satisfied by the time we get here; kept as a defensive
	// invariant against any submit that reaches this function without going
	// through the disabled button.
	detailsFieldErrors.terms = termsError(state.termsAccepted);
	if (detailsFieldErrors.terms) return;
	// The customer just edited the details behind a FAILED attempt. Without this,
	// walking forward from here landed them straight back on the old failure card
	// with no new request made at all: the machine was still parked on the failed
	// code, the pay step renders that card ahead of the review card, and the one
	// button that names the obvious action ("Initiate payment again") is disabled
	// on the codes this happens for. The corrected value sat in storage, unused,
	// and the only escape was a non-obvious detour through "Check payment status".
	//
	// flow.restart() is exactly the right instrument and needs no new safety
	// reasoning: it resets ONLY for codes that definitionally have no recoverable
	// payment behind them (canSafelyRestart), and preserves the attempt and its
	// recovery affordances for anything where money might exist. So a corrected
	// GSTIN gets a clean re-submit, and a declined card still cannot be silently
	// re-charged.
	if (pay.value.value !== S.REVIEW) flow.restart();
	goNext();
}

// The Review & Pay card's "Edit" affordance: return to Details WITHOUT touching
// the payment intent. If one already exists the subsequent Continue persists via
// update_billing; otherwise it just walks back to the card.
function editBilling() {
	state.billingEditReturn = true;
	state.step = "details";
}

// Persist a post-intent billing edit through the authenticated facade. Best-
// effort: a failure keeps the local snapshot (promise stays honest) and reports
// presence-only. billingEditAction is the single source of truth for "which
// path" so the choice is unit-tested (never guest signup).
async function saveBillingEdit() {
	if (billingEditAction(state.intentExists) !== "update_billing") return;
	try {
		const d = (await updateBilling(billing.buildBilling())) || {};
		billing.markBillingSaved(d.billing_saved === true);
	} catch (e) {
		reportError({ surface: "onboarding", error_code: "billing_update", message: "" });
	}
}

// ---- Pay: the strict payment state machine (plan 02 + plan-09 WS7) ----------
// The wizard consumes @/onboarding/usePaymentFlow - a pure reducer
// (paymentMachine) plus an orchestrator that owns every side effect - instead of
// the old tangle of payPhase/successData/provisioning flags and inline checkout
// code. The machine keys on the backend's CODE, never on HTTP status prose, and
// its ONE invariant is that nothing but an authoritative paid answer leaves the
// Pay page.
//
// plan-09 WS7 (the admin-hosted checkout cutover): the wizard opens NO gateway
// SDK on this origin. A payable answer carries a pay-page token plus the bench's
// OWN attested origin, and the customer is TOP-LEVEL NAVIGATED to
// `{origin}/jarvis-checkout#t=<token>` (flow.navigateToPay → the injected
// `navigate` below). There is no in-page sheet, no tenant-origin SDK, and no
// fallback: a token with no attested origin, or a pre-cutover admin's raw handles
// with no token, fails the step closed with honest copy.

// A session-scoped, non-secret marker that we are LEAVING this page for the
// admin-hosted pay page. On the way back - a bfcache restore or a tab regaining
// focus with checkout_open still frozen in memory - the pageshow/visibility
// handlers read it and drive the machine's explicit, safe RETURNED_FROM_CHECKOUT
// exit instead of leaving the customer on a permanent "Taking you to the secure
// payment page…" screen (plan 02 P0-2). STAMPED with the live attempt id (X3),
// not a bare "1": a leftover marker from attempt N must never drive a
// returnFromCheckout during attempt N+1.
function markExternalCheckoutNav(attemptId) {
	try {
		window.sessionStorage.setItem(CHECKOUT_NAV_KEY, String(attemptId || "1"));
	} catch (e) {
		/* private mode / storage disabled - the SPA return path still hydrates fresh */
	}
}
function readExternalCheckoutNav() {
	try {
		return window.sessionStorage.getItem(CHECKOUT_NAV_KEY) || "";
	} catch (e) {
		return "";
	}
}
function clearExternalCheckoutNav() {
	try {
		window.sessionStorage.removeItem(CHECKOUT_NAV_KEY);
	} catch (e) {
		/* no-op */
	}
}

// The admin-hosted pay page now sends the customer back here when the payment
// settles, rather than leaving them on a result screen whose only exit was a
// "Back to your workspace" button running history.back(). It appends
// `?pay=done|failed|pending`; this reads that hint and STRIPS it from the URL in
// the same breath, so a later reload cannot replay a verdict that has since moved
// on. The hint is advisory only - it never overrides server truth, it only stops
// a returning customer being shown the intro tour while the control plane catches
// up with the gateway.
const CHECKOUT_OUTCOME_PARAM = "pay";
const CHECKOUT_OUTCOMES = new Set(["done", "failed", "pending"]);
function readCheckoutOutcome() {
	let outcome = "";
	try {
		const url = new URL(window.location.href);
		const raw = url.searchParams.get(CHECKOUT_OUTCOME_PARAM) || "";
		if (CHECKOUT_OUTCOMES.has(raw)) outcome = raw;
		if (url.searchParams.has(CHECKOUT_OUTCOME_PARAM)) {
			url.searchParams.delete(CHECKOUT_OUTCOME_PARAM);
			window.history.replaceState(null, "", url.pathname + url.search + url.hash);
		}
	} catch (e) {
		/* no URL/history support: the reconcile below still runs on server truth */
	}
	return outcome;
}

const flow = createPaymentFlow({
	api: onboardingPaymentApi,
	// The DOM half of the top-level navigation (WS7): stamp the external-nav
	// marker with the live attempt id BEFORE the browser leaves, so the return
	// path can recognise this attempt's checkout, then same-tab navigate. The URL
	// is built by the flow/reducer from the bench's OWN attested origin + the
	// frozen `/jarvis-checkout` path + the token in the fragment - never a URL
	// admin supplied.
	navigate: ({ url, attemptId }) => {
		markExternalCheckoutNav(attemptId);
		window.location.assign(url);
	},
	// Persist the (attempt, generation) support-check count so a refresh between
	// checks does not reset the "offer a human" moment (P2-5). Non-secret only.
	storage: {
		get: (k) => {
			try {
				return window.localStorage.getItem(k);
			} catch (e) {
				return null;
			}
		},
		set: (k, v) => {
			try {
				window.localStorage.setItem(k, v);
			} catch (e) {
				/* no-op */
			}
		},
	},
	// PII-free transition telemetry (P2-3): forwarded to the admin error-report
	// surface. The payload is only the shape of the move (from/to/code/provider/
	// generation/bucket/source) - never email, company, address, a gateway
	// payload, a payment id, or a token. Ordinary transitions are capped SEPARATELY
	// from the shared error budget so a chatty payment page cannot crowd out real
	// errors (X6); illegal transitions bypass the cap and always report.
	telemetry: makeTelemetryReporter(reportError),
	// Plan 01 billing round-trip. The billing snapshot rides start_signup (see
	// onPayClick); admin echoes billing_saved:true only when it durably persisted
	// it, which flips the "kept with your account" promise and retires the local
	// snapshot. On resume, admin's state read carries the snapshot so a different
	// device rehydrates the Details form (server truth wins) and, being persisted,
	// proves an intent exists (billing edits then route through update_billing).
	onBillingSaved: (saved) => billing.markBillingSaved(saved),
	onServerBilling: (summary) => {
		if (billing.hydrateServerSnapshot(summary)) state.intentExists = true;
	},
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

// Server-truth identity for the resumed/recovery screens (C02-3 / P1-7: never the
// prefill). On any server-driven screen the recipient is admin's identity or an
// honest placeholder - never state.email unless the CUSTOMER typed it this
// session (identityFromUser), because a resumed page's prefill can be the SITE
// ADMIN's email. The fresh review/starting path may use what the user typed.
const payEmail = computed(() => {
	const server = pay.value.summary?.email;
	if (server) return server;
	const onServerScreen = pay.value.value !== S.REVIEW && pay.value.value !== S.STARTING_SIGNUP;
	if (onServerScreen) {
		return state.identityFromUser && state.email ? state.email : "your email address";
	}
	return state.email || "your email address";
});
// The identity a reconnect submits - server truth first, then the user's OWN
// typed values, NEVER the prefill (P1-7). Empty when neither exists, which
// disables the reconnect action until an identity is available.
const reconnectIdentity = computed(() => ({
	email: pay.value.summary?.email || (state.identityFromUser ? state.email.trim() : ""),
	company: pay.value.summary?.company || (state.identityFromUser ? state.company.trim() : ""),
}));
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
		rows.push({
			label: "Payment method",
			value: pay.value.provider === "cashfree" ? "Cashfree" : "Razorpay",
		});
	}
	if (s.dueTodayInr != null && !Number.isNaN(s.dueTodayInr)) {
		rows.push({
			label: "Amount",
			// inrExact, not inr: dueTodayInr is a real charge (server-computed,
			// GST-inclusive) and can carry paise precision - same reasoning as
			// planDueToday/the subtotal-GST-total rows above.
			value: paySummaryTrial.value ? "₹0 today" : inrExact(s.dueTodayInr),
		});
	}
	const ref = intentRef.value;
	if (ref) rows.push({ label: "Reference", value: ref });
	if (pay.value.lastCheckedAt) {
		rows.push({ label: "Last checked", value: relativeSince(pay.value.lastCheckedAt) });
	}
	return rows;
});
// The full attempt id, shown in full on the owner's explicit request (was
// masked to a `…`+last-6 tail before). attemptId is itself already an opaque
// per-attempt handle admin hands the bench for display - never a gateway order
// id or a document name (those still stay on admin's side, contract rule 3) -
// so showing it whole does not leak either.
const intentRef = computed(() => {
	return pay.value.attemptId || "";
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
// One tick's observation from the provisioning poll. Assigning a fresh object
// (rather than mutating) is what makes the computed phase re-evaluate.
function noteProvisioning(o) {
	provisioningSeen.value = {
		answered: !!(o && o.answered),
		tenantStatus: (o && o.tenantStatus) || "",
	};
}
async function recheckProvisioning() {
	if (recheckingSetup.value) return;
	recheckingSetup.value = true;
	setupRecheckNote.value = "";
	// A fresh look starts from "nothing observed": the stale phase from the wait
	// that already exhausted must not be what this one opens on.
	provisioningSeen.value = null;
	try {
		const out = await flow.waitForProvisioning({ onObservation: noteProvisioning });
		if (out.status === "ready") {
			state.step = "connect";
			return;
		}
		// Do not discard the outcome: a silent 90 seconds followed by the same
		// screen reads as a broken button. It reports what the re-check OBSERVED -
		// it deliberately no longer says "still preparing your workspace", which
		// asserted that preparation is ongoing when all the loop established is
		// that nothing reported ready.
		setupRecheckNote.value =
			out.status === "delayed"
				? "We checked again and your workspace still isn't ready. Your payment is complete, and you can leave this page and come back."
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

// What the LAST provisioning tick actually saw. NULL means no tick has reported
// yet; see readinessSeen for why that is deliberately distinct from a tick that
// answered nothing. "Jarvis is getting ready for you" must never be a default.
const provisioningSeen = ref(null);
const provisioningStage = computed(() =>
	provisioningSeen.value
		? provisioningPhase(provisioningSeen.value)
		: inFlightPhase("Checking on your workspace")
);
// jarvis#726: the progress bar next to the phase list above, reading the SAME
// provisioningStage - see waitPhases.phaseProgress.
const provisioningProgress = computed(() => phaseProgress(provisioningStage.value));

// The FULL-SCREEN busy view is only for the phases where there is genuinely
// nothing to press: starting the signup, the sheet being open, confirming.
// A status check or a retry deliberately does NOT hide the recovery card -
// plan 02 §a11y is explicit that both buttons must not be replaced by an
// indefinite spinner; they are disabled in place instead (see payActionDisabled).
const payBusyView = computed(
	() => pay.value.value === S.STARTING_SIGNUP || pay.value.value === S.CHECKOUT_OPEN
);
const checking = computed(() => pay.value.busy === "checking");
const initiating = computed(() => pay.value.busy === "initiating");
// The verify round trip OPENS the checkout on success, so an unguarded
// triple-click stacked three gateway sheets. The flow holds the same in-flight
// guard its siblings do; this is the half the customer can see.
const verifying = computed(() => pay.value.busy === "verifying");
// jarvis#297 P0-2a: "Resend the link" takes the SAME one-action lock as verify/
// check/initiate, so this and verifying can never both be true at once - see
// payActionDisabled, which disables every button on the card while either is.
const resending = computed(() => pay.value.busy === "resending");
// The payment-confirming window: the customer paid on the admin-hosted page,
// came back, and the bench is asking the control plane whether that payment
// landed. It is the one moment where money has genuinely left them and no
// verdict exists yet.
//
// Driven by an explicit view-owned flag rather than derived from the machine,
// for two reasons that both bite:
//
//   - `busy === "checking"` belongs to the "Check payment status" BUTTON
//     (beginAction("checking") in usePaymentFlow.checkStatus). The post-checkout
//     reconcile - reconcileAfterFailure, and hydrate's frozen-checkout exit -
//     never sets it, so keying this off it would have meant the screen
//     essentially never appeared on the one path it exists for.
//   - even if it did, plan 02 a11y is explicit that an explicit status check
//     must NOT have its buttons replaced by a full-screen indefinite spinner;
//     they are disabled in place on the recovery card instead. So this is the
//     return reconcile only, never any status check.
const confirmingReturn = ref(false);
const showConfirming = computed(() => !payBusyView.value && confirmingReturn.value);

// PROOF the customer actually finished checkout on this mount: the pay page appends
// `?pay=done` (payment confirmed) or `?pay=pending` (authorized, awaiting async
// confirmation - the auto-pay mandate case) after the Razorpay flow finishes
// (jarvis_admin_v2's billing/checkout/shell.py outcomeFor vocabulary), and onMounted's
// readCheckoutOutcome reads it. Set true for done OR pending - both prove the flow
// finished - and nothing else. It is the discriminator the calm settling hold
// (showPaymentSettling) gates on, because a pending state alone cannot tell a
// webhook-still-in-flight from a checkout the customer never touched (see paymentCodes.js
// PAYMENT_CONFIRMATION_PENDING). Without this proof we never soften the copy; a `failed`
// return does not soften it either. In-memory
// for the session: a hard reload strips the param and drops to the recovery card,
// which is honest (see PR notes), not a regression. Same for the signal-less return
// paths (bfcache restore / tab-focus regain -> handleCheckoutReturn): they carry no
// ?pay= outcome, so they cannot prove completion and keep the recovery card. The
// flag is reset to false whenever a checkout (re)opens - see the CHECKOUT_OPEN watch
// below - so proof from one attempt can never soften a later, unrelated wait.
const returnedFromCompletedCheckout = ref(false);

/** Hold the confirming screen for the duration of a post-checkout reconcile. */
async function whileConfirmingReturn(run) {
	confirmingReturn.value = true;
	try {
		return await run();
	} finally {
		confirmingReturn.value = false;
	}
}
// The pending states the background auto-poll covers AND the calm settling hold
// fronts. Kept as ONE predicate so the loop guards, restartPendingAutoPoll and the
// settling computed can never drift apart:
//   - S.UNKNOWN                 PAYMENT_CONFIRMATION_PENDING (webhook lag after checkout)
//   - S.CONFIRM_REQUIRED        PAYMENT_AUTHORIZED_PENDING_CONFIRM (bank confirming the
//                               auto-pay e-NACH mandate - the standard trial/auto-pay
//                               signup lands HERE, so it is the primary panic in prod)
// Both are "authorized/paid, admin just hasn't confirmed yet" waits, never a dead end.
// CONFIRM_REQUIRED can pend past the ceiling (e-NACH is slow by design); the ceiling +
// honest escalation below is exactly what bounds it, so the calm hold never hangs
// forever (the concern that originally kept this state out).
function isAutoPollable(v) {
	return v === S.UNKNOWN || v === S.CONFIRM_REQUIRED;
}
const showRecovery = computed(
	() =>
		!payBusyView.value &&
		!showConfirming.value &&
		// The calm settling hold owns the pending window right after a completed
		// checkout; recovery only takes over once that hold releases (poll gave up,
		// or the state moved on). Everything else here is unchanged.
		!showPaymentSettling.value &&
		(pay.value.value === S.UNKNOWN ||
			pay.value.value === S.FAILED_RETRYABLE ||
			pay.value.value === S.FAILED_TERMINAL ||
			pay.value.value === S.CONFIRM_REQUIRED ||
			pay.value.value === S.RECONNECT)
);
// The calm "we're confirming, hang tight" hold shown INSTEAD of the recovery card
// during the normal post-checkout wait. Conditions, all required:
//   - the customer actually completed checkout this session (returnedFromCompletedCheckout)
//   - the machine is on an auto-pollable pending state (isAutoPollable): the coded
//     PAYMENT_CONFIRMATION_PENDING / PAYMENT_AUTHORIZED_PENDING_CONFIRM waits, or a return
//     not yet resolved - the same states the auto-poll (runPendingAutoPoll) covers
//   - the auto-poll has NOT given up (!pendingPollStuck)
//   - we are not already showing the one-shot confirming spinner or a busy screen
const showPaymentSettlingCandidate = computed(
	() =>
		!payBusyView.value &&
		!showConfirming.value &&
		returnedFromCompletedCheckout.value &&
		!pendingPollStuck.value &&
		isAutoPollable(pay.value.value)
);
// HARD wall-clock ceiling on the calm hold. pendingPollStuck alone is NOT a reliable
// escape hatch: the server rate-limits the status check (every 429 adds a cooldown the
// poll must wait out) and browsers throttle background-tab timers, so the poll's "8
// checks" can take FAR longer than 2 minutes - a customer sat on the calm spinner for
// ~15 min in testing because pendingPollStuck never fired in time. This deadline flips
// the hold to the recovery card (with its manual "Check payment status" button) after
// ~2 min no matter what the poll is doing, so the calm hold can never trap the customer.
// Measured from when the hold first appears; a re-entry restarts the clock. The Pay-step
// cooldown ticker updates nowMs every 1s, so this escalates within ~1s of the ceiling.
const SETTLING_MAX_MS = 120_000;
const settlingStartedAt = ref(0);
watch(showPaymentSettlingCandidate, (on) => {
	settlingStartedAt.value = on ? Date.now() : 0;
});
const settlingExpired = computed(
	() => settlingStartedAt.value > 0 && nowMs.value - settlingStartedAt.value >= SETTLING_MAX_MS
);
const showPaymentSettling = computed(
	() => showPaymentSettlingCandidate.value && !settlingExpired.value
);
// The calm-screen copy, keyed off which pending state we are settling. The state's OWN
// coded headline is used only where it already reads calm: PAYMENT_AUTHORIZED_PENDING_CONFIRM
// is a real authorization at the gateway, so "Your payment is authorized" is both true and
// reassuring. PAYMENT_CONFIRMATION_PENDING's coded headline is the alarming "We have not
// confirmed this payment" - the whole point of this hold is to NOT show that during the
// normal wait, so it gets bespoke calm copy instead.
const settlingCopy = computed(() =>
	pay.value.value === S.CONFIRM_REQUIRED
		? {
				headline: "You're almost set up",
				body:
					"Your payment is authorized. We're waiting for a final confirmation from " +
					"your bank, which usually takes just a few minutes. Sit tight, we've got " +
					"it from here.",
		  }
		: {
				headline: "Confirming your payment",
				body:
					"You've completed checkout. We're waiting for your payment provider to confirm " +
					"it, which can take up to a minute, and we're checking automatically.",
		  }
);
// role=alert only for an actionable failure; role=status for pending/info
// (plan 02 §a11y - a pending payment announced as an alert on every poll is a
// flashing banner to a screen reader).
const recoveryRole = computed(() => (payCopy.value.tone === TONE.ALERT ? "alert" : "status"));
// The specific, customer-facing detail the machine captured (an SDK that would
// not load, a gateway that refused to open, admin's own sentence on a coded
// refusal). Suppressed when it merely repeats the coded copy.
const payDetail = computed(() => {
	// The customer-facing message admin/the codec attached to the current state
	// (e.g. a decline reason). Suppressed when it merely repeats the coded copy.
	const m = pay.value.message || "";
	if (!m) return "";
	return m === payCopy.value.body || m === payCopy.value.headline ? "" : m;
});
const provisioningDelayed = computed(() => pay.value.value === S.PROVISIONING_DELAYED);
// plan-09 07-c: with the rollout flag OFF, a fresh customer reaching the pay step
// sees a maintenance hold instead of the review/pay card - there is no old path to
// fall back to (owner decision 4). Only replaces the FRESH review card; a customer
// with real server state (paid, verifying, a recovery) still sees their own state.
const showMaintenanceHold = computed(() => !state.paymentUiV2 && pay.value.value === S.REVIEW);

// The busy-screen line. CHECKOUT_OPEN is the copy MOMENT before the browser
// top-level-navigates to the admin-hosted pay page (WS7).
const payBusyLabel = computed(() => {
	if (pay.value.value === S.STARTING_SIGNUP) return "Starting your signup…";
	return "Taking you to Jarvis' secure payment page…";
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

// jarvis#297 P0-2a: the post-resend cooldown, same live-clock shape as
// checkCountdown/checkLabel above - no reducer round trip needed to lift it,
// the SAME 1s ticker (active whenever state.step === "pay", which covers the
// verify screen) already recomputes this every tick.
const resendCountdown = computed(() => remainingResendCooldownSeconds(pay.value, nowMs.value));
const resendLabel = computed(() =>
	resendCountdown.value > 0
		? `Resend in ${resendCountdown.value}s`
		: ACTION_LABELS[ACTIONS.RESEND]
);

// The action buttons for the recovery card, in the table's order (status-first).
// The support affordance is appended when the client-local check ceiling is hit
// even if the code's own actions don't list it (a pending payment the customer
// has checked many times).
// True when this signup ALREADY has a checkout the customer can simply go back to:
// a live token, the bench's own origin, and admin's attestation that the two agree.
// Asked of the MACHINE via the same predicate the reducer's own NAVIGATED_TO_PAY
// guard uses, so the button and the guard can never drift apart - offering a
// navigate the reducer would refuse is how a customer ends up on a dead token.
const canResumeCheckout = computed(() => canNavigateToPay(pay.value));

const recoveryActions = computed(() => {
	const acts = [...payCopy.value.actions];
	// RESUME is prepended dynamically rather than listed on individual rows, because
	// navigability is a property of the LIVE TOKEN, not of the code that happens to
	// be showing: the same PAYMENT_CONFIRMATION_PENDING is resumable while the token
	// lives and is not, forty-five minutes later. Listing it per row would encode a
	// static answer to a question that is only ever true at a moment in time.
	//
	// Placed AFTER a CHECK the row asked for, and only otherwise first.
	//
	// RESUME must outrank INITIATE: reusing the checkout that already exists is
	// strictly safer and cheaper than minting another, which on Razorpay leaves the
	// previous subscription live and unreferenced (admin-v2#248).
	//
	// But it must NOT outrank CHECK, and an earlier version of this that unshifted
	// unconditionally did. PAYMENT_CONFIRMATION_PENDING can carry a live token AND
	// mean "money may already have moved" - its own body says "Check the status
	// before doing anything else." Making RESUME the primary button there walks
	// the customer back to the payment page ahead of the read that would tell
	// them they have already paid, which is precisely the double payment
	// status-first exists to prevent. A row that asks for CHECK first has a
	// reason; RESUME slots in behind it.
	if (canResumeCheckout.value && !acts.includes(ACTIONS.RESUME)) {
		const check = acts.indexOf(ACTIONS.CHECK);
		if (check >= 0) acts.splice(check + 1, 0, ACTIONS.RESUME);
		else acts.unshift(ACTIONS.RESUME);
	}
	if (pay.value.supportOffered && !acts.includes(ACTIONS.SUPPORT)) acts.push(ACTIONS.SUPPORT);
	return acts;
});

// jarvis#297 P0-2a: the verify screen's own action row. VERIFY (from the TABLE
// row) stays first/solid, unchanged. RESEND is spliced in - never listed
// statically, same reasoning as RESUME above - only once the machine's
// canResendVerification says the server actually granted it (fail-closed;
// see paymentMachine.js). RESTART (labelled "Use a different email" for this
// code) is always last: it is the one action VERIFY and RESEND can never
// substitute for.
const verifyActions = computed(() => {
	const acts = [...payCopy.value.actions]; // [VERIFY, RESTART]
	if (pay.value.canResendVerification && !acts.includes(ACTIONS.RESEND)) {
		acts.splice(1, 0, ACTIONS.RESEND);
	}
	return acts;
});

// X7 (defensive): the recovery card offers Reconnect but there is no identity to
// send it with. The contract always ships email+company, so this should not
// happen - but a dead disabled button is a worse failure than asking, so render
// inline fields instead. Typing them fills reconnectIdentity via identityFromUser.
const reconnectNeedsIdentity = computed(
	() =>
		recoveryActions.value.includes(ACTIONS.RECONNECT) &&
		(!reconnectIdentity.value.email || !reconnectIdentity.value.company)
);
function onReconnectIdentityInput() {
	// Mark the values as CUSTOMER-typed so reconnectIdentity may use them (never the
	// site-admin prefill, P1-7).
	state.identityFromUser = true;
}

// X8: when restart() refuses to reset (a payment is still recoverable behind the
// current state, P1-3), say so instead of leaving a silent no-op button.
const restartHeldNote = ref("");

// ---- "Check payment status" must always say something ----------------------
// The button used to call flow.checkStatus() and nothing else. That is fine when
// the answer is a payment state, because the machine repaints. It is NOT fine for
// the answer this signup actually gets when its start_signup was rejected before
// an intent existed: admin answers BENCH_NO_SIGNUP_CONTEXT, whose reducer branch
// resets the machine to a blank REVIEW card. The customer pressed a button, the
// screen silently became a fresh form, and nothing anywhere said why. That reads
// as a broken button, and it is the "check payment status not working" report.
const statusCheckNote = ref("");
async function runStatusCheck() {
	statusCheckNote.value = "";
	const before = pay.value.value;
	await flow.checkStatus();
	const after = pay.value.value;
	if (after === S.REVIEW && pay.value.notStarted) {
		statusCheckNote.value =
			"We checked, and there is no payment on this site to look up yet. Nothing has been charged. Enter your details and start the payment when you're ready.";
		return;
	}
	if (after === before && !pay.value.transportError) {
		// A real answer that changed nothing is still an answer. Saying so beats a
		// button that appears to do nothing at all.
		statusCheckNote.value = "We checked just now, and nothing has changed yet.";
	}
}

// ---- pending-payment auto-poll -----------------------------------------------
// Every other wait in the wizard polls on its own (provisioning 45x2s, readiness
// 40x3s); this recovery card used to do nothing until the customer clicked Check
// themselves. UNKNOWN covers PAYMENT_CONFIRMATION_PENDING (the coded wait) and a
// return from checkout that has not resolved yet - both are "ask again shortly"
// states, never a dead end.
//
// Gentler than the other waits ON PURPOSE: unlike readiness/provisioning,
// checkStatus asks the real payment gateway and the server rate-limits it
// (PAYMENT_CHECK_RATE_LIMITED) - so this reads the machine's own cooldown after
// every check and waits it out before the next attempt, instead of a fixed
// interval that could poll straight through a 429.
const PENDING_CHECK_INTERVAL_MS = 15_000;
// The FIRST re-check is deliberately early - but ONLY for a completed-checkout
// return (returnedFromCompletedCheckout). That is the one case with a physical
// reason to expect an answer within seconds: the confirmation webhook usually
// lands just behind the browser redirect, so this resolves it while the customer
// is still watching the calm settling screen, before the steady 15s cadence kicks
// in. An in-app pending wait that never left the page has no such reason, so it
// keeps the ordinary 15s first interval and does not hit the rate-limited endpoint
// any sooner than before (code review finding 3). The server's own cooldown
// (checkCooldownUntil, applied below) still protects against a 429 either way.
const PENDING_FIRST_CHECK_MS = 4_000;
const PENDING_CHECK_ATTEMPTS = 8; // ~4s + 15s x 7 ≈ 2 minutes
// Set once the ceiling is reached with no resolution - an honest "we stopped
// auto-checking" note, never a spinner that quietly gives up. Cleared the moment
// the state leaves UNKNOWN (resolved) or Pay is left (see restartPendingAutoPoll).
const pendingPollStuck = ref(false);
// Bumped on every (re)start AND on every stop, so a run already sleeping/awaiting
// checkStatus() can tell it has been superseded and quit without touching state
// that no longer belongs to it (leaving Pay, a fresh CONTRACT_STATE, unmount).
let pendingPollRun = 0;

async function runPendingAutoPoll(myRun) {
	for (let i = 0; i < PENDING_CHECK_ATTEMPTS; i++) {
		await _sleep(
			i === 0 && returnedFromCompletedCheckout.value
				? PENDING_FIRST_CHECK_MS
				: PENDING_CHECK_INTERVAL_MS
		);
		if (pendingPollRun !== myRun || !isAutoPollable(pay.value.value)) return;
		await flow.checkStatus();
		if (pendingPollRun !== myRun || !isAutoPollable(pay.value.value)) return;
		// A rate limit says nothing about the money (see paymentCodes.js) - wait
		// out the SERVER's own cooldown ON TOP OF the ordinary interval already
		// elapsed, so the next attempt cannot land inside it and poll straight
		// through a 429. checkCooldownUntil is whatever the machine is currently
		// holding (0 on an ordinary answer), so this is a no-op except right after
		// a rate limit.
		const waitMs = (pay.value.checkCooldownUntil || 0) - Date.now();
		if (waitMs > 0) {
			await _sleep(waitMs);
			if (pendingPollRun !== myRun || !isAutoPollable(pay.value.value)) return;
		}
	}
	if (pendingPollRun === myRun && isAutoPollable(pay.value.value)) {
		pendingPollStuck.value = true;
	}
}

// (Re)starts the poll if - and only if - the machine is genuinely waiting on
// this signup's payment AND the customer is looking at the Pay step. Called from
// both the value-watch below (a fresh CONTRACT_STATE) and the step-lifecycle
// watch (entering/leaving Pay), so either kind of change restarts or stops it
// the same way; idempotent to call when neither condition holds.
function restartPendingAutoPoll() {
	pendingPollRun += 1; // supersede whatever was already running
	pendingPollStuck.value = false;
	if (isAutoPollable(pay.value.value) && state.step === "pay") {
		runPendingAutoPoll(pendingPollRun);
	}
	// NOTE: the value-change watch fires restartPendingAutoPoll on a
	// S.UNKNOWN -> S.CONFIRM_REQUIRED transition too, resetting the loop to its first
	// tick. So a signup that passes through both waits can sit on the calm hold for up
	// to ~2 min PER state (~4 min total) before escalating. Acceptable: both are
	// genuine "authorized/paid, awaiting confirmation" waits, and the escalation is a
	// ceiling on each, never a forever-spinner.
}
watch(() => pay.value.value, restartPendingAutoPoll);
// Clear the completed-checkout proof the instant a (new or resumed) checkout opens.
// From CHECKOUT_OPEN, the ONLY thing that may re-arm the calm settling hold is a
// fresh ?pay=done full-navigation return (set in onMounted). This does two things:
//   - fixes staleness (code review finding 1): the flag can no longer carry proof
//     from an earlier attempt into a later, unrelated pending wait.
//   - keeps the signal-less return paths honest (code review bfcache/tab-focus gap):
//     handleCheckoutReturn -> flow.returnFromCheckout() transits CHECKOUT_OPEN and
//     carries NO ?pay= outcome, so it must NOT soften the copy - a customer who
//     alt-tabbed back from an unpaid sheet would otherwise be told "you've completed
//     checkout". With the flag cleared here, that path lands on the honest recovery
//     card, exactly as it does today. Softening it would need a completion signal we
//     do not have on that path (see returnedFromCompletedCheckout's declaration).
watch(
	() => pay.value.value,
	(v) => {
		if (v === S.CHECKOUT_OPEN) returnedFromCompletedCheckout.value = false;
	}
);

// ---- "Resend the link" (jarvis#297 P0-2a) -----------------------------------
// A truthful confirmation line, not a toast that could be missed off-screen -
// same reasoning as statusCheckNote above. Only says "sent" when the flow
// itself judged the answer a real send (flow.resendVerification's {sent}); a
// failed or rate-limited attempt says nothing rather than claim one, which is
// the whole point of building this against a real capability flag instead of
// a button that always claims success.
const resendNote = ref("");
async function runResendVerification() {
	resendNote.value = "";
	const { sent } = await flow.resendVerification();
	resendNote.value = sent ? `We sent a new link to ${payEmail.value}.` : "";
}

// ---- Contact support: an actual ticket, not a mailto -----------------------
// This action used to set window.location.href to a mailto: URL. On any machine
// with no mail client configured - which is most browsers, and every kiosk and
// most corporate desktops - clicking it did visibly nothing, which is the
// "contact support not working" report. It also carried no context, so even when
// it did open a composer the customer had to describe a failure they cannot see
// the internals of.
//
// The app already has a real ticket API (jarvis.support.api.create_ticket) behind
// @/api's supportCreateTicket, and the onboarding wizard runs authenticated, so
// there is nothing stopping us filing the ticket properly. Rendered INLINE rather
// than in a Dialog on purpose: a portaled dialog loses the jv-* palette vars this
// view binds on its own root (design.md §2.2), and this panel is not worth that
// risk.
const SUPPORT_EMAIL = "support@aerele.in";
const supportOpen = ref(false);
const supportBody = ref("");
const supportBusy = ref(false);
const supportTicket = ref("");
const supportErr = ref("");

// What support needs to act without a round trip, and nothing a customer would be
// alarmed to read: the coded state, the full attempt reference the recovery card
// already shows (opaque per-attempt handle, not a gateway id or document name),
// and admin's own sentence. No token, no gateway id, no payload.
//
// Step-aware since the panel now serves both Pay (jarvis#708 hoisted it out of
// that step) and Connect: a payment code means nothing on a stuck AI-connect
// wait, and a connect phase means nothing on a stuck payment.
const supportContext = computed(() => {
	if (state.step === "connect") {
		const rows = [
			`Step: ${state.step}`,
			`Connect phase: ${state.connectPhase || "none"}`,
			`Operation: ${currentOpId.value || "none"}`,
		];
		if (state.connectMessage) rows.push(`Detail: ${state.connectMessage}`);
		return rows.join("\n");
	}
	const rows = [
		`Step: ${state.step}`,
		`Payment state: ${pay.value.value || "unknown"}`,
		`Code: ${pay.value.code || "none"}`,
	];
	if (intentRef.value) rows.push(`Reference: ${intentRef.value}`);
	if (pay.value.message) rows.push(`Detail: ${pay.value.message}`);
	return rows.join("\n");
});

function openSupport() {
	supportErr.value = "";
	supportTicket.value = "";
	supportBody.value = "";
	supportOpen.value = true;
}

function closeSupport() {
	supportOpen.value = false;
}

async function sendSupport() {
	if (supportBusy.value) return;
	supportBusy.value = true;
	supportErr.value = "";
	try {
		const subject =
			state.step === "connect"
				? "Onboarding setup help"
				: `Onboarding payment help (${pay.value.code || "no code"})`;
		const body = `${supportBody.value.trim()}\n\n---\n${supportContext.value}`;
		const d = (await supportCreateTicket(subject, body)) || {};
		// The API answers {ok, data}; the ticket name is whatever data carries. Show
		// it when present, but a successful call with an unfamiliar shape is still a
		// success - never turn one into an error the customer has to act on.
		supportTicket.value = (d.data && (d.data.name || d.data.ticket)) || "sent";
	} catch (e) {
		// Filing failed. Fall back to the address, visibly, so the customer is never
		// left with a dead button again - which was the whole point of this change.
		supportErr.value = errMsg(e);
	} finally {
		supportBusy.value = false;
	}
}

function payActionLabel(a) {
	if (a === ACTIONS.CHECK) return checkLabel.value;
	// The countdown wins the same way checkLabel's does above (jarvis#297 P0-2a).
	if (a === ACTIONS.RESEND) return resendLabel.value;
	// actionLabelFor lets a row override a label whose shared wording would mislead
	// in its own context - "Start again" on a details rejection, where the only
	// thing being started again is one corrected field; "Use a different email" on
	// the verify screen, where nothing else is being restarted either.
	return actionLabelFor(payCopy.value, a);
}
function payActionDisabled(a) {
	// Any mutating/checking call in flight disables every action on the CURRENT
	// card (plan 02: server idempotency, not button state, is what prevents
	// duplicate intents/mails - but a double-click should still not fire twice).
	// A status check disables itself and the retry, so an impatient customer
	// cannot stack concurrent provider-truth calls into the rate limit; verify
	// and resend take the SAME one action lock (usePaymentFlow.beginAction), so
	// they disable the same way (jarvis#297 P0-2a).
	if (checking.value || initiating.value || verifying.value || resending.value) return true;
	if (a === ACTIONS.CHECK) return !pay.value.canCheck;
	if (a === ACTIONS.INITIATE) return !pay.value.canInitiate;
	// RESUME is gated on the machine alone, never on a backend capability flag:
	// it creates nothing, so there is no capability to grant. It is offered only
	// while canNavigateToPay holds, and that is re-evaluated on every answer, so a
	// token that dies mid-screen removes the button rather than disabling it.
	if (a === ACTIONS.RESUME) return !canResumeCheckout.value;
	// Reconnect needs a real identity to send (P1-7): server truth or what the
	// customer themselves typed - never the site-admin prefill. Disabled until one
	// exists.
	if (a === ACTIONS.RECONNECT) {
		return !reconnectIdentity.value.email || !reconnectIdentity.value.company;
	}
	// RESEND is capability-gated (fail closed, so this only matters once the
	// backend starts granting it - verifyActions already hides the button
	// otherwise) AND cooled down client-side after a send, so the button cannot
	// be spammed into a burst of mail while the first one is still landing.
	if (a === ACTIONS.RESEND) {
		return !pay.value.canResendVerification || resendCountdown.value > 0;
	}
	return false;
}
function payActionLoading(a) {
	if (a === ACTIONS.CHECK) return checking.value;
	if (a === ACTIONS.INITIATE) return initiating.value;
	if (a === ACTIONS.VERIFY) return verifying.value;
	if (a === ACTIONS.RESEND) return resending.value;
	return false;
}
// `list` defaults to the recovery card's own actions; the verify screen passes
// its own verifyActions so VERIFY (always first there) is the one rendered
// solid, the same "whichever appears first is primary" rule (jarvis#297 P0-2a).
function payActionVariant(a, list = recoveryActions.value) {
	// Status-first: the primary (solid) action is whichever appears first, which
	// the copy table already orders as Check where double-payment is possible.
	// RESUME is unshifted to the front when it applies, so it becomes primary on
	// exactly the screens where going back to the existing checkout is the cheapest
	// and safest thing the customer can do.
	return list[0] === a ? "solid" : "subtle";
}
async function onPayAction(a) {
	if (a === ACTIONS.CHECK) return runStatusCheck();
	if (a === ACTIONS.RESUME) {
		// Straight back to the checkout this signup already has. No API call, no new
		// intent, no new provider object: the flow rebuilds the URL from the token
		// and origin already in the machine and top-level-navigates. It refuses on
		// its own if the state stopped being navigable between render and click.
		flow.navigateToPay();
		return;
	}
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
	if (a === ACTIONS.RESEND) return runResendVerification();
	if (a === ACTIONS.BILLING) {
		// Hard navigation: AppShell's gate re-renders the poster over an in-SPA route.
		window.location.assign("/jarvis/billing");
		return;
	}
	if (a === ACTIONS.SUPPORT) return openSupport();
	if (a === ACTIONS.RESTART) {
		// A details rejection is not a restart in any meaningful sense: admin named a
		// field the customer typed wrongly, and the reset below wipes the machine (and
		// with it admin's sentence) on the way to the Details step. Carry the sentence
		// across so the step they land on says WHY they are there, instead of looking
		// like the wizard threw their progress away for no reason.
		if (pay.value.code === CODES.BENCH_SIGNUP_DETAILS_REJECTED) {
			state.detailsErr = pay.value.message || payCopy.value.body;
			// The partner-code input lives inside a collapsed disclosure (closed by
			// default, since most customers have none). A walk-back landing here
			// because of the partner code must open it, or the field the customer
			// needs to fix stays invisible. Primary signal is local, not the message
			// text: the customer already typed a code, so revealing the (optional,
			// harmless-to-show) disclosure is correct even if this particular
			// rejection turns out to be about a different field. The /partner code/i
			// match on admin's free-text sentence is kept only as a secondary
			// signal - it still opens the disclosure for a customer who somehow
			// lands here with the field blank (jarvis#821 review: the text-only
			// match silently missed a reworded/translated rejection message; no
			// machine code exists yet for which field specifically failed).
			if (state.partnerCode?.trim() || /partner code/i.test(state.detailsErr)) {
				state.partnerCodeOpen = true;
			}
		}
		// jarvis#297 P0-2a: "Use a different email" on a signup this session TYPED
		// through Details already has state.email/company live in memory, but a
		// customer who RELOADED mid-verification (the exact dead end the issue
		// names) has none - only the machine's summary carries server truth, and
		// restart() below is about to wipe it. Copy it across first, and only into
		// a field the customer has not already typed something else into (never
		// clobber a live edit, same rule prefillAccount uses).
		if (pay.value.code === CODES.SIGNUP_VERIFICATION_REQUIRED) {
			const summary = pay.value.summary || {};
			if (summary.email && !state.email.trim()) {
				state.email = summary.email;
				state.identityFromUser = true;
			}
			if (summary.company && !state.company.trim()) state.company = summary.company;
		}
		// Server-truth-gated reset (P1-3): the flow resets the machine only when no
		// recoverable payment can be behind the current code, otherwise it preserves
		// the attempt and its recovery affordances. Editing details is offered only
		// when the reset actually happened - a preserved state keeps the customer on
		// their recovery card (status/reconnect/support), never on a fresh review
		// that would re-run start_signup over live money.
		const { reset } = flow.restart();
		if (reset) {
			restartHeldNote.value = "";
			state.step = "details";
		} else {
			// The reset was refused: a payment is still recoverable behind the current
			// state (P1-3). Say so, rather than a button that appears to do nothing (X8).
			restartHeldNote.value =
				"There's a payment we're still confirming, so we're keeping you here.";
		}
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
		// A state change means the machine moved on: drop the stale "we're keeping you
		// here" note so it never lingers past the state it explained (X8), and the
		// same for the resend confirmation (jarvis#297 P0-2a) - it stays up while
		// the customer remains on THIS verify screen and disappears once they leave
		// it (verified, or restarted onto a fresh one).
		restartHeldNote.value = "";
		resendNote.value = "";
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
	// plan-09 07-c: the CTA is hidden behind the maintenance hold when the flag is
	// off; guard the handler too so a stray/programmatic click cannot start a signup.
	if (!state.paymentUiV2) return;
	// Plan 01: the normalized billing snapshot rides this first signup call so it
	// persists server-side while the customer is still a guest. Omitted (undefined,
	// never an empty object) when nothing was entered, so a blank Details step sends
	// no billing key at all.
	const billingPayload = billing.buildBilling();
	if (!state.planName || !state.email || !state.company || !billingPayload.contact_number) {
		// A signup with empty args would create a broken record upstream. This is
		// the fresh-start guard; a resumed session renders from server truth and
		// uses Initiate, not this button. Contact number is checked here too
		// (jarvis#888): a resumed session can land directly on Pay with a
		// pre-change saved state that never collected it, and the server now
		// requires it - catch that before the throw, same as the other fields.
		state.detailsErr =
			"Your signup details are missing. Please go back and pick a plan and enter your details again.";
		state.step = "details";
		return;
	}
	// Belt-and-suspenders: Details' Continue is already gated on this (the real
	// gate now), but a programmatic/stray click must not be able to start a
	// signup admin will reject anyway for lacking acceptance.
	if (!state.termsAccepted) return;
	await flow.submitReview({
		email: state.email,
		company: state.company,
		plan: state.planName,
		provider: state.paymentProvider,
		billing: Object.keys(billingPayload).length ? billingPayload : undefined,
		// Top-level kwarg, parallel to nothing else here - NOT part of `billing`.
		// Trimmed + undefined-when-blank so a blank field sends no key at all.
		partner_code: state.partnerCode?.trim() || undefined,
		// T&C + lead-capture contract: only ever true here (Details' Continue
		// already required it). contact_consent is granted BY the T&C
		// acceptance itself (owner decision 2026-08-14), so it is the same
		// literal true, recorded at the exact moment identity is real.
		terms_accepted: true,
		contact_consent: true,
	});
	// Plan 01: a successful signup left REVIEW for a live intent (verify / checkout /
	// provisioning / duplicate). An intent now exists, so a subsequent Review & Pay
	// "Edit" saves through the authenticated update_billing facade, never a fresh
	// guest signup that would create/replace the intent.
	if (pay.value.value !== S.REVIEW) state.intentExists = true;
}

function cancelReconnect() {
	state.step = state.reconnectFrom || "pay";
	state.payErr = "";
	state.reconnectRequestId = "";
	state.reconnectCode = "";
	state.reconnectResentIn = 0;
	state.reconnectDirect = false;
	state.reconnectEmail = "";
}

// "I have a code from support": jump straight to the code screen in DIRECT mode
// WITHOUT asking admin to mail a code (there is no customer-started request). The
// operator already verified identity out of band and issued the code; the
// customer redeems it here with their registered email. Reachable from Details and
// from the emailed-code screen (a customer handed a support code instead).
function enterReconnectDirect() {
	state.payErr = "";
	state.reconnectCode = "";
	state.reconnectRequestId = "";
	state.reconnectResentIn = 0;
	state.reconnectEmail = reconnectIdentity.value.email || state.email || "";
	state.reconnectFrom = state.step === "reconnect" ? state.reconnectFrom : state.step;
	state.reconnectDirect = true;
	state.step = "reconnect";
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
		const id = reconnectIdentity.value;
		const d = await startAccountReconnect(id.email, id.company);
		state.reconnectRequestId = (d && d.request) || state.reconnectRequestId;
		// Keep the reuse tracker on the latest request (resend supersedes the old id).
		if (d && d.request) {
			reconnectIssued = { email: id.email, company: id.company, requestId: d.request };
		}
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
	if (state.reconnectDirect && !state.reconnectEmail.trim()) return;
	state.payErr = "";
	state.payBusy = true;
	try {
		// Direct mode redeems an operator-issued code with the registered email (no
		// request_id); the emailed-request path polls the request the customer
		// started. Both land through _land_reconnect, so the outcomes below are
		// identical — only the redeeming call differs.
		const d = state.reconnectDirect
			? await redeemReconnectCode(state.reconnectCode.trim(), state.reconnectEmail.trim())
			: await checkAccountReconnect(state.reconnectRequestId, state.reconnectCode.trim());
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
		if (d && d.status === "resume_payment") {
			// Recovered an UNFINISHED checkout, not a live workspace (admin-v2 #162):
			// there is no container, so the Connect shortcut above would strand this
			// customer on a workspace that never appears. What the code bought them is
			// the ability to AUTHENTICATE, which is the one thing the resume path was
			// missing - so hand off to the same mount reconciliation a reload runs and
			// let server truth place the step.
			state.payBusy = false;
			await reconcileMidFlightSignup();
			// Reconciliation lands a mid-signup customer on Pay from real state. If it
			// established none (a control-plane blip), Details is still forward progress
			// and never a spinner - the credentials are persisted either way.
			if (state.step === "reconnect") state.step = "details";
			return;
		}
		if (d && d.status === "renew_payment") {
			// Recovered a LAPSED account (Expired sub): its container was stopped on expiry, so
			// there is nothing to ride sync_connection to AND no unfinished checkout to resume. The
			// code re-authenticated this bench onto the existing account; hand off to the billing
			// page, which owns the renew flow (pick a plan -> renew() reactivates the EXISTING
			// subscription + restarts the container -> the bounded recovery poll). A hard nav, like
			// ACTIONS.BILLING: AppShell's gate re-renders the poster over an in-SPA route.
			state.payBusy = false;
			window.location.assign("/jarvis/billing");
			return;
		}
		// Direct mode has no confirmation page (the code came from support) and its
		// invalid covers a mistyped code OR the wrong registered email, so point the
		// customer at both — never at a screen they never saw.
		if (state.reconnectDirect) {
			state.payErr = "That code or email didn't match. Check both and try again.";
		} else {
			state.payErr =
				d && d.status === "expired"
					? "The reconnect request expired. Start it again."
					: "That code didn't match. Check the confirmation page and try again.";
		}
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
	// No re-entry while a request is in flight (would mint a second code + burn the 5/hr budget).
	if (state.payBusy) return;
	state.payErr = "";
	state.detailsErr = ""; // any prior Details error is stale once we commit to reconnect
	state.reconnectCode = "";
	state.reconnectDirect = false;
	state.reconnectEmail = "";
	// Authoritative identity only (P1-7): server truth, or what the customer
	// themselves typed - never the site-admin prefill.
	const id = reconnectIdentity.value;
	// Where to return on Back/cancel: reconnect can be entered from Details
	// (before any plan is chosen), from the recovery card, and from the review
	// card's can_reconnect offer.
	state.reconnectFrom = state.step === "reconnect" ? state.reconnectFrom : state.step;
	// Cancel -> Continue on the SAME identity reuses the outstanding request (admin caps 5/hr); a stale code -> "Resend".
	if (
		reconnectIssued &&
		reconnectIssued.email === id.email &&
		reconnectIssued.company === id.company &&
		reconnectIssued.requestId
	) {
		state.reconnectRequestId = reconnectIssued.requestId;
		state.step = "reconnect";
		return;
	}
	state.payBusy = true;
	try {
		const d = await startAccountReconnect(id.email, id.company);
		state.reconnectRequestId = (d && d.request) || "";
		reconnectIssued = {
			email: id.email,
			company: id.company,
			requestId: state.reconnectRequestId,
		};
		state.step = "reconnect";
	} catch (e) {
		state.payErr = errMsg(e);
		// Details has no payErr banner + the gate hid the fallback, so surface it here (no silent dead-end).
		if (state.step === "details") state.detailsErr = state.payErr;
	} finally {
		state.payBusy = false;
	}
}

// ---- Connect: the ONE durable apply-operation controller --------------------
// "Start chatting" is a single awaited transaction (plan-05 D2, review §8/§10.4).
// The editor persists the desired pool and hands back a durable apply-operation
// descriptor; ONE createOperationController follows exactly that operation to a
// terminal state, and navigation to Chat happens exactly once, only on an
// authoritative `ready`. Every non-ready terminal (retry, rejected, superseded,
// timeout) stays on THIS step with a real recovery action - there is no "continue
// anyway" escape hatch into a chat that cannot answer (review P0-01/P0-08).
//
// The router's first-run guard (router/index.js) memoizes its readiness probe in a
// module-level readyPromise; forgetReady() clears that memo so router.replace re-runs
// the guard fresh and reaches Chat - no full-page reload needed (review P0-01/P1-06).

// Small awaitable delay used by the controller's bounded readiness/resume loops.
function _sleep(ms) {
	return new Promise((r) => setTimeout(r, ms));
}

const poolRef = ref(null);
const savingConnect = ref(false);
// True once the embedded editor reports a savable config (account connected, or an
// API key filled) - gates the "Start chatting" button (@ready from the editor). The
// STRICTER start gate (a passing probe for a freshly-typed remote key) is enforced in
// saveConnect via the editor's exposed canStart, with an inline reason when it refuses.
const connectReady = ref(false);
// Mirrors LlmPoolEditor's own subscription Test (subscription-testing emit): while
// it is running, "Start chatting" is disabled - the two would otherwise push the
// same desired pool through two independent idempotency keys at once. The editor's
// hostBusy prop (bound to savingConnect below) is the other half of this guard.
const subscriptionTesting = ref(false);

// Persist ONLY opaque handles across a reload: the operation id (operationStore) and
// the idempotency key. Never a credential, never a token.
const opStore = operationStore();
const IDEM_STORE_KEY = "jarvis.llm_apply.idempotency_key";
function rememberIdem(key) {
	try {
		if (key) sessionStorage.setItem(IDEM_STORE_KEY, key);
	} catch (e) {
		/* private-mode / quota: resume is best-effort */
	}
}
function recallIdem() {
	try {
		return sessionStorage.getItem(IDEM_STORE_KEY) || "";
	} catch (e) {
		return "";
	}
}
function forgetIdem() {
	try {
		sessionStorage.removeItem(IDEM_STORE_KEY);
	} catch (e) {
		/* ignore */
	}
}
function newIdemKey() {
	try {
		if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
	} catch (e) {
		/* fall through to the timestamp+random fallback */
	}
	return `idem-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

// The single controller, the op it is currently following, and a one-shot navigation
// guard so a duplicate terminal (or a re-entry) can never navigate twice.
let opController = null;
const currentOpId = ref("");
const navigated = ref(false);
let retryTimer = null;

// jarvis#752: the chat_readiness_reason the most recent poll carried, held
// across the follow so the deadline can quote it. Needed because the
// controller's own 5-minute deadline synthesizes a bare timedOut status with no
// reason on it at all - see createOperationController's tick() - so the only
// place left to read admin's last word from is what the ticks already told
// onOpUpdate. It TRACKS that value, including back to empty: a reason that later
// cleared is no longer true, and quoting it at a timeout would be a confident
// wrong answer (round-1 review). Reset on a genuinely fresh attempt (saveConnect,
// chooseDifferentModel); a Retry re-follows the SAME operation, so what it last
// said is still about the config on screen.
let lastOpChatReadinessReason = "";

// The setup screen's headline, following the live phase (jarvis#727) instead of
// the fixed "Setting up {agentName}" that used to sit above a phase list which
// had already become real (jarvis#722). setupHeadline owns every honesty
// decision; this only supplies what it cannot know: the brand name, and the
// fact that a ready verdict arrived and we are now navigating.
const setupTitle = computed(() =>
	setupHeadline(readinessStage.value, agentName, { navigating: navigated.value })
);

// jarvis#727. True once THIS attempt has watched the pipeline stall or fail on
// the AI connection the customer chose, which is the only evidence that makes
// "use a different model" an honest offer rather than a guess.
//
// The bar is deliberately an OBSERVATION, not a reason code, because the reason
// codes alone cannot carry it: `llm_pool_provisioning` is a perfectly ordinary
// mid-apply state for the first ninety seconds and a permanent dead end at the
// hundred and twentieth, and nothing in the code distinguishes those. What does
// distinguish them is that a bounded wait ran to its ceiling while admin kept
// answering. So this is set at exactly three places:
//
//   - a readiness wait that reached its ceiling having HEARD from admin
//     (sawVerdict) - admin kept telling us the config was still outstanding and
//     it never converged;
//   - an apply-operation deadline that DID confirm the operation in flight
//     (!neverConfirmed) - the operation to wire this model in existed and never
//     finished;
//   - an operation-level RETRY - admin reported a real failure applying it.
//
// and deliberately NOT when nothing was observed: a wait that never once
// reached admin (sawVerdict false), a deadline where no poll ever confirmed
// anything (neverConfirmed), and the generic support dead-end all know only
// that we could not ask. Offering a model change there would imply the model is
// at fault, which is a diagnosis nobody made - the same class of claim
// jarvis#709 removed. Those states keep Retry, which CAN help them, plus
// Contact support.
//
// Also never on the three stopping verdicts (waitPhases `stop`): a paged
// authority repair, a paused subscription and a moved account all render the
// `blocked` panel, and a different model provably cannot resolve any of them.
const connectModelChangeOffered = ref(false);

// Return to the editable Connect form in place, with the customer's own choice
// still in it. The editor is v-show'd, never v-if'd, so its state - the provider,
// the model, a passing key probe - is still mounted and simply becomes visible
// again; there is nothing to re-fetch or re-fill. `blockReason` is the inline
// banner above the editor that says why they are back and what to do next.
//
// Shared by both in-wizard escapes out of the wait: the jarvis#727 model-change
// (chooseDifferentModel) and slice 4b's Reconnect CTA (reconnectFromStall). It has
// to be in-wizard for both. Settings is the obvious home for "reconnect / add a
// model", but it is unreachable from here by construction: `llm_pool_provisioning`,
// `llm_provisioning` and `readiness_unconfirmed` are all in readiness.js's
// NOT_ONBOARDED_REASONS, so AppShell's `showGate` renders the full-screen
// onboarding poster over every route except this one. A "go to Settings" link
// would put the customer back on this wizard by a longer path.
//
// The forgets are load-bearing, not tidying. The customer is about to submit a
// FRESH configuration, so this attempt's idempotency key must not survive: admin
// dedupes on it and would hand back the very operation that never converged.
// currentOpId must go with it, or retryConnect would re-follow that dead operation
// instead of saving the new config (enterSaveRefusal, the one terminal that can be
// reached next, never clears it).
function returnToConnectForm(blockReason) {
	stopRetryCountdown();
	forgetIdem();
	opStore.forget();
	currentOpId.value = "";
	forgetReady();
	readinessSeen.value = null;
	opReadinessDetail.value = "";
	lastOpChatReadinessReason = "";
	state.connectPhase = "";
	state.connectTitle = "";
	state.connectMessage = "";
	state.connectPaged = false;
	state.connectSupportOffered = false;
	state.retryAfter = 0;
	connectModelChangeOffered.value = false;
	state.connectBlockReason = blockReason;
	// Any wait still sleeping between polls stops on its next tick (both loops
	// re-check this), so a late ceiling cannot yank the customer back out of the
	// form they were just returned to.
	state.finishing = false;
}

// The jarvis#727 escape. The block reason says what was observed - the wait ended
// without setup finishing - and never that the chosen model is broken, which
// nothing here established.
function chooseDifferentModel() {
	returnToConnectForm(
		"Setup didn't finish with the AI connection you chose, and waiting hasn't cleared it. Pick a different model and start again."
	);
}

function ensureController() {
	if (opController) return opController;
	opController = createOperationController({
		poll: (id) => getLlmApplyOperation(id),
		onUpdate: onOpUpdate,
		store: opStore,
		isVisible: () => typeof document === "undefined" || document.visibilityState !== "hidden",
		onVisible: (cb) => {
			const h = () => {
				if (typeof document === "undefined" || document.visibilityState !== "hidden") cb();
			};
			if (typeof document !== "undefined") document.addEventListener("visibilitychange", h);
			return () => {
				if (typeof document !== "undefined")
					document.removeEventListener("visibilitychange", h);
			};
		},
	});
	return opController;
}

function stopRetryCountdown() {
	if (retryTimer) {
		clearInterval(retryTimer);
		retryTimer = null;
	}
}
function startRetryCountdown() {
	stopRetryCountdown();
	if (!(state.retryAfter > 0)) return;
	retryTimer = setInterval(() => {
		state.retryAfter = Math.max(0, state.retryAfter - 1);
		if (state.retryAfter <= 0) stopRetryCountdown();
	}, 1000);
}

// Side-effect-free render hook: project each operation phase into the setup screen's
// copy. Navigation lives in onTerminal, never here.
function onOpUpdate(ui) {
	const phase = ui && ui.phase;
	// jarvis#752: mirror THIS tick's chat_readiness_reason for the live phase row
	// (blank when this tick named nothing, never a stale earlier one - see
	// opReadinessDetail), and separately remember the last NON-EMPTY one this
	// attempt has seen, for the deadline timeout onTerminal renders with no ui of
	// its own to read from.
	opReadinessDetail.value = (ui && ui.chatReadinessReason) || "";
	// Round-1 review: TRACKS the live value rather than ratcheting to the newest
	// non-empty one. Only holding onto non-empty reasons meant a condition that
	// later CLEARED (a quota reset, say) while something else kept the operation
	// from finishing would still be quoted at the deadline, giving the customer a
	// confidently wrong diagnosis at the exact moment they most need a right one.
	// A blank tick is admin saying it no longer has a reason, which is itself the
	// truth, so honour it. Kept as a separate variable because onTerminal's
	// timeout branch synthesizes a bare status with no ui to read from.
	lastOpChatReadinessReason = (ui && ui.chatReadinessReason) || "";
	if (phase === OP_PHASE.REJECTED) {
		// The input is the problem: return to the editable form with the reason.
		// This IS the jarvis#727 escape, already built, for the one case admin can
		// name outright - so there is nothing left to offer.
		state.finishing = false;
		state.connectPhase = "rejected";
		connectModelChangeOffered.value = false;
		state.connectBlockReason =
			(ui && ui.message) ||
			"That AI configuration was rejected. Edit your key or connection and try again.";
		return;
	}
	state.finishing = true;
	if (phase === OP_PHASE.FINISHING) {
		state.connectPhase = "finishing";
		state.connectMessage = "";
		state.finishSubtitle = "Saved. Finishing setup, this can take a minute.";
	} else if (phase === OP_PHASE.RETRY) {
		state.connectPhase = "retry";
		state.connectMessage = (ui && ui.message) || "We hit a snag applying your AI connection.";
		state.connectTitle = connectHeadline("retry", { fromReadinessCeiling: false });
		state.retryAfter = (ui && ui.retryAfterSeconds) || 0;
		// Admin reported a real failure applying THIS configuration (jarvis#727).
		// Retry stays the primary action - the failure is transient by
		// classification - but it is no longer the only one.
		connectModelChangeOffered.value = true;
		startRetryCountdown();
	} else if (phase === OP_PHASE.SUPERSEDED) {
		state.connectPhase = "superseded";
		state.connectMessage =
			(ui && ui.message) ||
			"Your workspace assignment changed. Reload this page and try again.";
		state.connectTitle = connectHeadline("superseded");
		// A stale offer from an EARLIER readiness wait (jarvis#708) must not survive
		// into this unrelated terminal - supersession means reload, not "still not
		// resolved", and this phase already has its own recovery action. The
		// jarvis#727 offer is withdrawn for the same reason, and one more: a newer
		// save owns the truth now, so this screen no longer knows which
		// configuration is even being applied.
		state.connectSupportOffered = false;
		connectModelChangeOffered.value = false;
	} else {
		// WORKING (pending / applying), and the descriptor seed.
		state.connectPhase = "working";
		state.connectMessage = "";
		state.finishSubtitle = "We'll take you to chat as soon as your setup is done.";
	}
}

// jarvis#840: the ONE preflight between "readiness turned green" and chat. All
// three arrival paths (waitForChatReadiness, followLegacyReadiness, the durable
// operation's onTerminal) converge on navigateToChat, so gating here covers the
// instant-green case that motivated the issue - a perfectly-wired connect whose
// FIRST message then died upstream on a quota 429 nothing had checked.
//
// Verdict policy (the issue's core constraint): only a genuine credential
// rejection ("auth") blocks - back to the connect form with the verbatim
// reason. A provider usage limit shows an honest line for a beat and proceeds;
// unchecked/unknown/unreachable rows NEVER block; a preflight call that itself
// fails is skipped entirely (fail open - this gate must not be able to strand
// a customer the old readiness flow would have let through).
const preflight = reactive({
	running: false,
	done: false,
	plugin: "",
	persona: "",
	usable: null,
	notice: "",
});
const PREFLIGHT_NOTICE_MS = 2500;

async function runPreflightGate() {
	if (preflight.running) return false;
	preflight.running = true;
	// The checklist renders inside the working screen, so make sure it shows
	// even when readiness was instantly green and no wait was ever displayed.
	state.finishing = true;
	if (!state.connectPhase) state.connectPhase = "working";
	let d = null;
	try {
		d = await runChatPreflight();
	} catch (e) {
		d = null;
	}
	preflight.running = false;
	preflight.done = true;
	preflight.plugin = (d && d.plugin) || "unchecked";
	preflight.persona = (d && d.persona) || "unchecked";
	preflight.usable = (d && d.usable) || { state: "unknown", detail: "" };
	if (preflight.usable.state === "auth") {
		// The connection itself was rejected upstream: the fix is in the
		// customer's hands, so land them back on the editable form with the
		// provider's own sentence. A fresh attempt re-runs the preflight.
		//
		// The forgets mirror chooseDifferentModel's and are load-bearing, not
		// tidying (jarvis#840 review B1): the customer is about to submit a
		// FIXED credential, so this attempt's idempotency key and operation id
		// must not survive - admin dedupes on both and would hand straight
		// back the very operation whose credential the probe just refused,
		// re-blocking forever. Stale terminal copy goes with them.
		preflight.done = false;
		stopRetryCountdown();
		forgetIdem();
		opStore.forget();
		currentOpId.value = "";
		forgetReady();
		readinessSeen.value = null;
		opReadinessDetail.value = "";
		lastOpChatReadinessReason = "";
		state.connectPhase = "";
		state.connectTitle = "";
		state.connectMessage = "";
		state.connectPaged = false;
		state.connectSupportOffered = false;
		state.retryAfter = 0;
		connectModelChangeOffered.value = false;
		state.connectBlockReason =
			preflight.usable.detail ||
			"Your AI provider rejected this connection. Reconnect your account.";
		state.finishing = false;
		return false;
	}
	if (preflight.usable.state === "rate_limit") {
		// Honest, non-blocking: the plan is out of quota, chat still opens.
		preflight.notice =
			"Your AI plan is at its usage limit right now. Chat will respond again when it resets.";
		await _sleep(PREFLIGHT_NOTICE_MS);
		// The customer may have left the wizard during the notice beat; a
		// navigation fired after unmount would yank a different view around.
		if (preflightDisposed.value) return false;
	}
	return true;
}
// Unmount cancels a pending notice-beat navigation (jarvis#840 review); the
// operation controller's own unmount abort does not cover this local sleep.
const preflightDisposed = ref(false);
onUnmounted(() => {
	preflightDisposed.value = true;
});

// The three checklist rows the preflight owns (jarvis#840), rendered between
// the readiness row and "Opening chat": waiting -> active (the one call in
// flight) -> done/unknown. No row state ever blocks - the blocking "auth"
// verdict leaves this screen entirely (runPreflightGate), so it never renders
// as a row.
const preflightRows = computed(() => {
	const rowState = (v) => {
		if (preflight.running) return "active";
		if (!preflight.done) return "waiting";
		return v === "ok" ? "done" : "unknown";
	};
	const suffix = (v) =>
		!preflight.done || v === "ok"
			? ""
			: v === "unchecked"
			? ": not checked"
			: ": needs attention";
	const usable = (preflight.usable && preflight.usable.state) || "";
	const usableLabel = !preflight.done
		? "AI connection answers a live check"
		: usable === "ok"
		? "AI connection checked"
		: usable === "rate_limit"
		? "AI connection is at its usage limit"
		: usable === "timeout"
		? "The live check timed out"
		: "AI connection: no live check ran";
	return [
		{
			key: "plugin",
			state: rowState(preflight.plugin),
			label: `Business tools wired${suffix(preflight.plugin)}`,
		},
		{
			key: "persona",
			state: rowState(preflight.persona),
			label: `Assistant persona loaded${suffix(preflight.persona)}`,
		},
		{ key: "usable", state: rowState(usable), label: usableLabel },
	];
});

// The six connect-wait steps as ONE labeled bar (2026-08-14 redesign; see the
// template comment for why the phase columns are gone). Short labels sit above
// the segments; `explain` is the sentence the line under the bar shows while
// that step is current. Nothing here is invented: step 1 is settled by
// reaching this screen (the save was accepted), step 2 reads readinessStage
// (waitPhases.readinessPhase has no DONE branch - readiness counts as done
// exactly when the preflight it hands off to has started), steps 3-5 read the
// jarvis#840 preflight rows verbatim, and "Chat" turns active only once the
// preflight resolved, which is also when navigateToChat runs.
const connectSteps = computed(() => {
	const rows = preflightRows.value;
	const readinessDone = preflight.running || preflight.done;
	return [
		{
			id: "saved",
			label: "Connection",
			state: "done",
			explain: "Your AI connection is saved.",
		},
		{
			id: "workspace",
			label: "Workspace",
			state: readinessDone ? "done" : readinessStage.value.state,
			explain: readinessStage.value.label,
		},
		{ id: "plugin", label: "Tools", state: rows[0].state, explain: rows[0].label },
		{ id: "persona", label: "Persona", state: rows[1].state, explain: rows[1].label },
		{ id: "check", label: "Live check", state: rows[2].state, explain: rows[2].label },
		{
			id: "chat",
			label: "Chat",
			state: preflight.done ? "active" : "waiting",
			explain: "Opening your chat.",
		},
	];
});

// Current step = the first one not done; `indeterminate` keeps the honesty
// rule waitPhases.phaseProgress drew: an UNKNOWN current step (a poll that
// answered with nothing, or a degraded non-blocking preflight row) pulses
// instead of filling. While the single preflight call is in flight all three
// of its rows are active at once, so the explanation collapses them into one
// sentence rather than naming a row the call does not check first.
const connectProgress = computed(() => {
	const steps = connectSteps.value;
	// Always hits: the chat step is never "done" (only active or waiting), so
	// a first not-done step always exists; the clamp is a defensive fallback,
	// not a state this reaches.
	const rawIndex = steps.findIndex((s) => s.state !== "done");
	const index = rawIndex === -1 ? steps.length - 1 : rawIndex;
	const explain = preflight.running
		? "Running final checks on your setup."
		: steps[index].explain;
	return {
		index,
		indeterminate: steps[index].state === "unknown",
		explain,
		// Names the current step (2026-08-16 redesign) so the caption alone
		// carries what the removed per-step labels used to say.
		caption: `Step ${index + 1} of ${steps.length} · ${steps[index].label}`,
	};
});

// Navigate to Chat exactly once, only on an authoritative ready. forgetReady() clears
// the router's memoized readiness so its guard re-checks fresh, and router.replace
// re-runs that guard → Chat.
async function navigateToChat() {
	if (navigated.value) return;
	// A terminal that resolves AFTER the customer escaped back to the editable
	// form (the jarvis#727 chooseDifferentModel path sets finishing=false and
	// never aborts the in-flight follow) must not yank the screen into a
	// preflight over the abandoned config, let alone bill a probe against it
	// (PR #848 review). Only the wait screen owns navigation.
	if (!state.finishing && !preflight.running) return;
	if (!preflight.done) {
		const proceed = await runPreflightGate();
		if (!proceed) return;
		if (navigated.value) return;
	}
	navigated.value = true;
	stopRetryCountdown();
	forgetIdem();
	opStore.forget();
	forgetReady();
	// Onboarding is over, so the transitional local billing snapshot has finally
	// outlived its purpose. This is where the "kept in this browser for now" promise
	// is honoured - NOT at the durable-write ack, which fires while the customer is
	// still mid-flow and used to destroy the only copy that could survive the
	// checkout round trip (see useBillingDetails.markBillingSaved).
	billing.finish();
	router.replace({ name: "Chat" });
}

// The AI applied but the workspace is not chat-ready yet. Poll readiness (a read,
// cheap, no mutation) and navigate the moment it turns true. Bounded, and on
// expiry it lands on the SAME honest retry state every other non-ready terminal
// gets - never a silent navigation into a chat that cannot answer.
// One poll's observation from the readiness wait, projected into the phase the
// setup screen renders. Same discipline as noteProvisioning: a poll that threw
// reports answered:false and gets copy that claims nothing.
function noteReadiness(o) {
	const seen = {
		answered: !!(o && o.answered),
		reason: (o && o.reason) || "",
		detail: (o && o.detail) || "",
	};
	readinessSeen.value = seen;
	const stage = readinessPhase(seen);
	// jarvis#757: a hard rejection (admin permanently refused this config; see
	// readinessPhase's "llm_rejected" case) is EARLIER than the jarvis#752
	// operation-verdict case above and unlike the three "blocked" stop-cases
	// below - the fix IS in the customer's hands (the connection they chose), so
	// this returns to the EDITABLE form with admin's own reason, mirroring
	// exactly what onOpUpdate's OP_PHASE.REJECTED branch already does for the
	// pool/operation path (jarvis#727). Never Retry: retrying the identical
	// rejected config cannot succeed, which is the false promise this state
	// exists to stop making.
	if (stage.editable) {
		state.finishing = false;
		state.connectPhase = "rejected";
		connectModelChangeOffered.value = false;
		state.connectBlockReason =
			stage.detail ||
			"That AI configuration was rejected. Edit your connection and try again.";
		return stage;
	}
	// Slice 4b (C10b): a subscription-connect strand that waiting cannot heal, but
	// one the customer CAN act on - unlike the paged/suspended/moved blocks below.
	// Its own phase so the panel carries a primary Reconnect CTA into the wizard's
	// reconnect entry instead of the support-only dead end, mirroring how `editable`
	// routes llm_rejected back to the form. `state.finishing` stays true so this card
	// (which lives under the setup screen's v-show) renders in place of the spinner.
	// Admin's own sentence (stage.detail) is the body; no model change, because a
	// rejected model is not the problem here.
	if (stage.reconnect) {
		state.connectPhase = "reconnect";
		state.connectTitle = stage.title || "Your AI subscription needs reconnecting";
		state.connectMessage = stage.detail;
		connectModelChangeOffered.value = false;
		return stage;
	}
	// A verdict that waiting cannot resolve is terminal for this wait: stop
	// polling rather than counting down to a ceiling whose copy invites a retry
	// that cannot help. Covers a paged authority repair (do nothing, we called
	// someone), a paused subscription, and an account that has moved to another
	// site - the last two need the CUSTOMER to act, so they must not be dressed
	// in the "our team has been notified" reassurance.
	if (stage.stop) {
		state.connectPhase = "blocked";
		state.connectTitle = stage.title || "We couldn't continue setting up";
		state.connectMessage = stage.detail;
		state.connectPaged = !!stage.paged;
		state.connectSupportOffered = true;
		// Not one of these three is a model problem: a paged authority repair must
		// see no self-service action at all, a paused subscription needs a renewal,
		// and a moved account needs the other site. Offering a different model here
		// would be a wrong diagnosis dressed as help (jarvis#727).
		connectModelChangeOffered.value = false;
	}
	// Returned so the wait loops can accumulate what their polls NAMED, not just
	// that the polls answered. See sawNamedSubject in waitForChatReadiness.
	return stage;
}

const CHAT_READY_ATTEMPTS = 40;
const CHAT_READY_INTERVAL_MS = 3000;
async function waitForChatReadiness() {
	// What this run actually observed, so the exhaustion message below can say
	// exactly that and nothing more (jarvis#708) - see readinessWaitExhaustedMessage.
	let sawVerdict = false;
	// Whether any poll of THIS wait NAMED what it was waiting on (jarvis#727).
	// Deliberately not sawVerdict, which the review round showed is far too coarse
	// to hang a diagnosis on: it means only "a poll returned JSON", and
	// `readiness_unconfirmed` is a perfectly well-formed 200 whose documented
	// meaning in jarvis/account.py is that admin COULD NOT BE ASKED. Gating the
	// model-change offer on sawVerdict therefore told a customer their chosen
	// connection had failed on the strength of a wait in which nothing about that
	// connection was ever established - the exact false claim this feature exists
	// to avoid, and a contradiction of this function's own ceiling comment.
	// A named subject is `kind !== NONE`, i.e. admin said the outstanding work was
	// the LLM apply or the container. "At least once", never "on the last poll":
	// ninety seconds of llm_pool_provisioning followed by one transient unconfirmed
	// is still a wait that watched this configuration fail to converge.
	let sawNamedSubject = false;
	let lastDetail = "";
	// A new wait starts from "nothing observed yet", so a stale phase from an
	// earlier attempt is never the first thing this one renders.
	readinessSeen.value = null;
	for (let i = 0; i < CHAT_READY_ATTEMPTS; i++) {
		if (navigated.value) return;
		// No pre-tick "blocked" guard: a blocked verdict is set by noteReadiness only
		// alongside stage.stop, and this loop already returns on stage.stop in the same
		// tick that observes it (below), so it can never sleep and re-enter here blocked.
		// The customer took the jarvis#727 / slice-4b escape back to the editor. This
		// wait is about the configuration they just left behind, so its ceiling must not
		// fire and pull them back out of the form.
		if (!state.finishing) return;
		// The memoized verdict is what the router guard will read moments from now, so
		// it has to be dropped before each probe or this loop polls a cached answer.
		forgetReady();
		let r = null;
		try {
			r = await isReadyForChat();
		} catch (e) {
			// A readiness call that throws is not a verdict. Keep waiting.
		}
		if (r) {
			sawVerdict = true;
			if (r.detail) lastDetail = r.detail;
		}
		if (r && r.ready) {
			navigateToChat();
			return;
		}
		// Render what THIS poll saw. Previously every one of these 40 answers was
		// discarded except the last detail, so the screen showed one fixed
		// sentence for two minutes and a customer could not tell a workspace
		// being built from one that was stuck.
		const stage = noteReadiness({
			answered: !!r,
			reason: r && r.reason,
			detail: r && r.detail,
		});
		if (stage.kind !== PHASE_KIND.NONE) sawNamedSubject = true;
		// Any verdict waiting cannot resolve is terminal for this wait (stage.stop):
		// the paged/suspended/moved blocks, jarvis#757's editable llm_rejected (which
		// already returned to the form), and slice 4b's reconnect stop. Stop polling
		// THIS tick rather than sleeping once more on a state that will not change on
		// its own. `stage.stop` is exactly this set, so it replaces the older
		// connectPhase/editable pair without altering their behaviour.
		if (stage.stop) return;
		await _sleep(CHAT_READY_INTERVAL_MS);
	}
	state.finishing = true;
	state.connectPhase = "retry";
	// jarvis#709: built from what this run OBSERVED, never from elapsed time.
	// Unchanged.
	state.connectMessage = readinessWaitExhaustedMessage({ sawVerdict, detail: lastDetail });
	// The headline used to be a hard-coded "Still finishing setup" in the
	// template, which asserted the very progress the message above refuses to
	// claim. It is now derived from the same source.
	state.connectTitle = connectHeadline("retry", { fromReadinessCeiling: true });
	state.connectSupportOffered = true;
	// jarvis#727: this is the exact state the issue was filed from. Admin named the
	// outstanding work for this configuration and never said Ready, so Retry
	// re-runs a thing this run watched fail to converge. Offer the model change
	// beside it. When nothing was ever named - admin unreachable, or reachable but
	// unable to answer (readiness_unconfirmed) - only Retry and support are honest.
	connectModelChangeOffered.value = sawNamedSubject;
	state.retryAfter = 0;
}

// The terminal handler for a followed operation. READY → navigate once; every other
// terminal (or a deadline timeout) stays here with a recovery action.
function onTerminal(status) {
	stopRetryCountdown();
	if (!status) {
		enterSupport();
		return;
	}
	if (status.timedOut) {
		state.finishing = true;
		state.connectPhase = "retry";
		// A deadline is the absence of a completion, not a reported failure.
		state.connectTitle = connectHeadline("retry", { fromReadinessCeiling: true });
		if (status.neverConfirmed) {
			// jarvis#690: every poll for the whole deadline failed to reach admin -
			// nothing was ever confirmed applying, so "still finishing on its own" is
			// not honest (it implies progress that was never observed, and can hide a
			// save that never landed at all). Say what actually happened instead.
			state.connectMessage =
				"We couldn't reach your AI provider's setup service, so nothing has been confirmed. Please retry.";
		} else {
			// The deadline released us, not the job. jarvis#709 removed the phrase
			// "it's still finishing on its own" from the readiness wait because it
			// asserted continuing progress nothing had observed; this branch is the
			// last place it survived. It is better grounded here (polls DID confirm
			// the operation in flight before the deadline elapsed) but it is still a
			// present-tense claim made from a past observation, so it is anchored to
			// when that observation happened instead of projected forward.
			state.connectMessage =
				"Setup was still running when we last checked, and it's taking longer than usual. You can keep waiting and retry.";
			// jarvis#752: quote admin's own last word, if this attempt ever got one.
			// The deadline itself carries no reason (createOperationController's
			// synthetic timedOut status has none to read), so this is the one place
			// left that remembers it. Never reworded, only punctuation-closed, same
			// idiom as readinessWait.js's readinessWaitExhaustedMessage.
			const lastReason = lastOpChatReadinessReason.trim();
			if (lastReason) {
				const closed = /[.!?]$/.test(lastReason) ? lastReason : `${lastReason}.`;
				state.connectMessage += ` The last thing we heard: ${closed}`;
			}
		}
		// jarvis#727. The operation that wires the chosen model in was CONFIRMED in
		// flight and still did not finish inside its deadline, which is the state
		// tenant e4qdeprp4r sat in (LLM_APPLY_PENDING, applied_version behind
		// desired_version, forever). A different model is a real way out of that.
		// neverConfirmed is the opposite case - nothing was ever reached, so nothing
		// points at the configuration - and keeps Retry alone.
		connectModelChangeOffered.value = !status.neverConfirmed;
		state.retryAfter = 0;
		return;
	}
	const ui = classifyOperation(status);
	if (ui.canNavigate) {
		navigateToChat();
		return;
	}
	// The AI connection applied, but admin says the workspace is not chat-ready yet
	// (chat_readiness === false - typically the serving container still coming up).
	// This used to navigate anyway, because canNavigate only looked at the operation
	// state, and the customer landed in a chat that could not answer with nothing on
	// screen to explain it. Nothing is wrong here, so it is not a failure state: wait
	// for readiness and then go, with an honest line about what is happening.
	if (ui.awaitingChatReadiness) {
		state.finishing = true;
		state.connectPhase = "working";
		state.finishSubtitle =
			ui.chatReadinessReason ||
			"Your AI is connected. We're waiting on the last of your setup…";
		waitForChatReadiness();
		return;
	}
	// A non-navigable terminal. onOpUpdate already rendered the matching phase; a
	// rejected/superseded attempt is DONE, so the next Start mints a fresh operation -
	// drop the idempotency key. A retry re-follows the SAME op, so its key is kept.
	if (ui.phase === OP_PHASE.REJECTED || ui.phase === OP_PHASE.SUPERSEDED) forgetIdem();
}

function enterSupport() {
	state.finishing = true;
	state.connectPhase = "support";
	state.connectMessage =
		"We couldn't finish setting up your AI connection. Please try again in a moment.";
	state.connectTitle = connectHeadline("support");
	// A support dead-end is terminal for THIS attempt (F1/F8): drop the idempotency
	// key so the next Start mints a fresh one. Otherwise a poisoned key (e.g. a 409
	// IdempotencyKeyConflict, or an old-admin descriptor-less response) would make
	// every subsequent Retry re-submit the same conflicting key and dead-end again.
	forgetIdem();
	// This dead-end is unrelated to a chat-readiness wait (jarvis#708): don't let a
	// stale offer from an earlier one show under a different failure's message.
	state.connectSupportOffered = false;
	// And no model-change offer (jarvis#727). Everything that lands here - a
	// controller that threw, a null terminal, a resume that ran out, a
	// descriptor-less refusal - is a failure to complete the round trip, not an
	// observation about the configuration. Retry is the honest primary here
	// because the round trip is exactly what may work next time.
	connectModelChangeOffered.value = false;
}

// Follow a descriptor (or a bare op id on resume) to its terminal state. Supersession
// / unmount rejects with {aborted:true}, which is not an error to surface.
async function followDescriptor(descriptorOrId) {
	currentOpId.value =
		typeof descriptorOrId === "string"
			? descriptorOrId
			: (descriptorOrId && descriptorOrId.operation_id) || "";
	state.finishing = true;
	// Third entry point into the "working" screen, and the one that was missing the
	// invariant the two wait loops each state at their own top: a new attempt must
	// not open on the LAST attempt's observation. Reached by Retry, which re-follows
	// the same operation - so without this, a Retry taken from a readiness ceiling
	// re-rendered that ceiling's stale phase as the live one, and jarvis#727 wired
	// that phase into the h1, making a stale reading the biggest text on the screen.
	readinessSeen.value = null;
	state.connectPhase = "working";
	let terminal;
	try {
		terminal = await ensureController().follow(descriptorOrId);
	} catch (e) {
		if (e && e.aborted) return; // superseded or unmounted: expected, not an error
		enterSupport();
		return;
	}
	onTerminal(terminal);
}

// Turn a save_llm_pool RESULT into a terminal outcome. Precedence (backend contract,
// refined 2026-08-03):
//   apply_operation present               → follow that ONE durable operation.
//   resumable (op exists under same key)  → re-call save with the SAME idempotency
//                                           key, bounded, to obtain the descriptor.
//   mode === "legacy"                     → a single BYO api-key model whose admin
//                                           creds endpoint mints NO operation: fall
//                                           back to a bounded readiness poll (fail-
//                                           closed, no bypass).
//   else (operation path, null op, not    → a save refusal. A save-level rate-limit
//   resumable)                              cooldown (retry_after_seconds) is shown
//                                           truthfully; otherwise a support state.
// Never a silent ready.
const RESUME_MAX = 3;
async function resolveAndFollow(result, idem) {
	let r = result;
	let tries = 0;
	while (r && !r.apply_operation) {
		if (r.resumable) {
			if (tries >= RESUME_MAX || !poolRef.value) {
				enterSupport();
				return;
			}
			tries += 1;
			const again = await poolRef.value.save(idem); // same key → admin dedupes
			if (!again || !again.ok) {
				enterSupport();
				return;
			}
			r = again.result;
			continue;
		}
		if (r.mode === "legacy") {
			await followLegacyReadiness(r);
			return;
		}
		// Operation path, no descriptor, not resumable: a refusal (e.g. a rate limit).
		enterSaveRefusal((r && r.retry_after_seconds) || 0);
		return;
	}
	await followDescriptor(r.apply_operation);
}

// Legacy fallback (single-model direct config, mode:"legacy" - a BYO api-key model
// or a lone chat subscription): admin's creds endpoint mints no durable operation,
// so there is nothing to follow - poll readiness instead, bounded and fail-closed.
// The bound is the save result's readiness_budget_s (75s default, 300s for the
// dual-restart subscription leg) - see legacyReadyAttempts. On ready → navigate
// once; on timeout / persistent not-ready → the SAME support/retry state a
// non-navigable terminal gets (stay on Connect, offer Retry). Deliberately NO
// "continue anyway" bypass (review P0-08).
const LEGACY_READY_ATTEMPTS = 30;
const LEGACY_READY_INTERVAL_MS = 2500;
// The save result may carry readiness_budget_s (seconds) for a leg that takes
// longer than the 30x2.5s=75s default - today only the subscription direct leg,
// which does two container restarts back to back (see onboarding.py). Absent /
// invalid = the historic 75s, so the api_key/oauth legs and any old backend are
// byte-identical to before. The 2.5s interval is unchanged, so only the attempt
// count grows: 300s budget -> 120 attempts.
function legacyReadyAttempts(result) {
	const budgetS = Number(result && result.readiness_budget_s);
	if (!Number.isFinite(budgetS) || budgetS <= 0) return LEGACY_READY_ATTEMPTS;
	return Math.max(LEGACY_READY_ATTEMPTS, Math.ceil((budgetS * 1000) / LEGACY_READY_INTERVAL_MS));
}
async function followLegacyReadiness(result) {
	const attempts = legacyReadyAttempts(result);
	state.finishing = true;
	state.connectPhase = "working";
	state.finishSubtitle = "We'll take you to chat as soon as your setup is done.";
	// What this run actually observed, so the exhaustion message below can say
	// exactly that and nothing more (jarvis#708) - see readinessWaitExhaustedMessage.
	let sawVerdict = false;
	// See waitForChatReadiness for why the model-change offer is gated on a NAMED
	// subject and not on sawVerdict (jarvis#727 review round).
	let sawNamedSubject = false;
	let lastDetail = "";
	readinessSeen.value = null;
	for (let i = 0; i < attempts; i++) {
		if (navigated.value) return;
		// No pre-tick "blocked" guard - see waitForChatReadiness: stage.stop below ends
		// the wait in the same tick a blocked/stop verdict is observed.
		// The jarvis#727 / slice-4b escape took the customer back to the editor - see
		// waitForChatReadiness for why this wait must not outlive it.
		if (!state.finishing) return;
		let r = null;
		try {
			r = await isReadyForChat();
		} catch (e) {
			// transient: a readiness call that throws is not a verdict - keep polling
		}
		if (r) {
			sawVerdict = true;
			if (r.detail) lastDetail = r.detail;
		}
		if (r && r.ready) {
			navigateToChat();
			return;
		}
		// Same per-poll phase projection as waitForChatReadiness: this wait is just
		// as long and was just as silent.
		const stage = noteReadiness({
			answered: !!r,
			reason: r && r.reason,
			detail: r && r.detail,
		});
		if (stage.kind !== PHASE_KIND.NONE) sawNamedSubject = true;
		// See waitForChatReadiness: stage.stop is every verdict waiting cannot resolve
		// (blocked stops, jarvis#757's editable, slice 4b's reconnect).
		if (stage.stop) return;
		if (i < attempts - 1) await _sleep(LEGACY_READY_INTERVAL_MS);
	}
	state.finishing = true;
	state.connectPhase = "retry";
	state.connectMessage = readinessWaitExhaustedMessage({ sawVerdict, detail: lastDetail });
	state.connectTitle = connectHeadline("retry", { fromReadinessCeiling: true });
	state.connectSupportOffered = true;
	// Same ceiling, same rule as waitForChatReadiness (jarvis#727).
	connectModelChangeOffered.value = sawNamedSubject;
	state.retryAfter = 0;
}

// A save that was REFUSED before any operation opened. A rate-limit carries a cooldown
// (retry_after_seconds): honour it truthfully and never auto-resubmit. currentOpId
// stays empty, so retryConnect re-runs saveConnect (reusing the persisted key) once the
// cooldown elapses.
function enterSaveRefusal(retryAfterSeconds) {
	state.finishing = true;
	if (retryAfterSeconds > 0) {
		state.connectPhase = "retry";
		state.connectMessage =
			"Too many changes in a short time. Please wait a moment, then retry.";
		// Every other terminal sets this; without it this one rendered the generic
		// "We couldn't confirm your setup" over a rate-limit body, or worse, a
		// STALE title left behind by an earlier attempt.
		state.connectTitle = connectHeadline("retry", { fromReadinessCeiling: false });
		// Explicit for the same reason connectTitle is (jarvis#727 review round). A
		// rate limit is a refusal to accept the save AT ALL, so nothing about the
		// configuration was observed and no model change is indicated. It happens to
		// be false already on both live call paths, but only because each caller
		// resets it first - an invariant held by the callers is not one this panel
		// can rely on, and every sibling terminal sets it here rather than assume.
		connectModelChangeOffered.value = false;
		state.retryAfter = retryAfterSeconds;
		startRetryCountdown();
	} else {
		enterSupport();
	}
}

// The sole controller. One click = one save = one followed operation; a second click
// while in flight is a no-op (the savingConnect guard + the controller's own
// supersession). The idempotency key is minted once per attempt and persisted BEFORE
// the save, so a lost response can resume by re-calling save with the same key.
async function saveConnect() {
	if (savingConnect.value || navigated.value) return;
	if (!poolRef.value) return;
	// Require a savable + validated config: subscription → a capture/stored account;
	// remote api_key → a passing probe bound to the current fields; local → fields set.
	if (!poolRef.value.canStart) {
		state.finishing = false;
		state.connectPhase = "";
		state.connectBlockReason =
			poolRef.value.startBlockedReason || "Connect a model to continue.";
		return;
	}
	state.connectBlockReason = "";
	// A genuinely fresh attempt: any earlier attempt's "we couldn't confirm this,
	// get a person to look into it" offer was about THAT attempt, not this one.
	// Same for the jarvis#727 model-change offer, which is an observation about a
	// configuration this attempt has not yet tried. Same for the jarvis#752 last-
	// heard reason: it described the operation this attempt is about to replace.
	state.connectSupportOffered = false;
	connectModelChangeOffered.value = false;
	lastOpChatReadinessReason = "";
	// Round-1 review: the LIVE detail must clear here too, not only the
	// remembered one. A timeout ends an attempt through onTerminal, which never
	// calls onOpUpdate, so nothing blanks it. Until this new operation's first
	// tick lands, readinessStage falls to its else branch and would render the
	// PREVIOUS attempt's verdict, describing a credential this attempt has
	// already replaced. Showing a stale verdict is the exact failure this change
	// exists to remove.
	opReadinessDetail.value = "";
	savingConnect.value = true;
	try {
		let idem = recallIdem();
		if (!idem) {
			idem = newIdemKey();
			rememberIdem(idem); // persist BEFORE save so a lost response can resume
		}
		const res = await poolRef.value.save(idem);
		if (!res || !res.ok) {
			// A validation / persist error keeps the customer on the editable form; the
			// attempt never opened an operation, so the key can be dropped.
			state.finishing = false;
			state.connectPhase = "";
			state.connectBlockReason =
				(res && res.error) || "We couldn't save your AI connection. Please try again.";
			forgetIdem();
			return;
		}
		await resolveAndFollow(res.result, idem);
	} finally {
		savingConnect.value = false;
	}
}

// Recovery action shared by the retry / timeout / support states. It re-FOLLOWS the
// same operation (never a second save) when one exists; otherwise (an idem-only lost
// response, or a support state before any op) it re-runs saveConnect, which reuses the
// persisted idempotency key so admin dedupes.
function retryConnect() {
	if (state.retryAfter > 0) return; // honour the cooldown
	state.connectMessage = "";
	if (currentOpId.value) {
		followDescriptor(currentOpId.value);
		return;
	}
	saveConnect();
}

// Superseded: the current operation is dead; the honest recovery is a clean reload
// that re-follows whatever operation now owns the truth.
function reloadConnect() {
	window.location.reload();
}

// Slice 4b (C10b): the Reconnect CTA on the stalled-subscription STOP card. It
// re-opens the editable Connect form IN PLACE so the customer can re-run the
// subscription connect right here - success navigates to chat, a genuine re-strand
// brings this card back, a too-old host fails clean (S1). It deliberately does NOT
// hard-nav to RECONNECT_INTENT_URL (the old behaviour): every customer who can see
// this card is already terminal (they reached PAID -> PROVISIONING -> connect), and
// landingStep DROPS the reconnect intent when terminal, so that nav resumed straight
// back to "connect", restarted the wait, re-observed reconnect_required and
// re-rendered this exact card - an infinite no-op loop. The fix is local to this
// step; landingStep's terminal guard is correct for the ChatView site_replaced flow
// and is left alone. Same fresh-attempt reset as chooseDifferentModel (returnToConnectForm
// drops the stalled operation so the next Start is genuinely new).
function reconnectFromStall() {
	returnToConnectForm(
		"Reconnect your AI subscription below, then start again to finish setting up."
	);
}

// (An unreferenced `editConnect` lived here: a partial return-to-the-form that
// nothing ever called, because onOpUpdate's REJECTED branch inlines its own.
// Removed rather than reused by jarvis#727's escape - it dropped neither the
// idempotency key nor the operation id, so wiring it to a button would have sent
// the customer's NEW configuration under the old attempt's key and had admin
// dedupe it straight back to the stuck operation.)

// Resume an in-flight apply on mount (reload mid-apply): follow the SAME operation
// rather than showing an editable form over a running one (review P1-05). If only the
// idempotency key survived (the save response was lost before the op id was stored),
// recover the descriptor by re-calling save with that key once the editor has reloaded
// the saved config.
async function maybeResumeConnect() {
	const opId = opStore.recall();
	if (opId) {
		state.step = "connect";
		state.reconciledConnect = true; // no local pay context on a resume
		navigated.value = false;
		await followDescriptor(opId);
		return;
	}
	const idem = recallIdem();
	if (!idem) return;
	state.step = "connect";
	state.reconciledConnect = true;
	navigated.value = false;
	// Wait (bounded) for the connect editor to mount and reload the persisted config so
	// its save() rebuilds the same payload the lost attempt sent.
	for (let i = 0; i < 40 && !(poolRef.value && poolRef.value.canStart); i++) {
		await _sleep(100);
	}
	if (!(poolRef.value && poolRef.value.canStart)) {
		// Couldn't auto-resume: leave the editable form. A fresh Start supersedes the
		// orphaned operation, so drop the stale key.
		forgetIdem();
		return;
	}
	const res = await poolRef.value.save(idem);
	if (res && res.ok) await resolveAndFollow(res.result, idem);
	else forgetIdem();
}

// Enter-step triggers: load the plan list on reaching "plan" (first entry
// from the tour, or a "Back" from details). No gateway SDK is preloaded (or
// loaded at all): the Pay step top-level-navigates to the admin-hosted checkout
// (plan-09 WS7), so there is no tenant-origin SDK to warm up.
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
			// A fresh wait starts from "nothing observed yet", so a stale phase from
			// an earlier run can never be the first thing this one shows.
			provisioningSeen.value = null;
			const out = await flow.waitForProvisioning({ onObservation: noteProvisioning });
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
// This watcher also owns the Pay-step lifecycle (plan 02 P1-8): leaving Pay
// invalidates any in-flight client work (a status reconcile still running after
// a dismissal, a confirm poll) so its late response cannot reroute a hidden
// component; re-entering Pay hydrates fresh server truth.
watch(
	() => state.step,
	(s, prev) => {
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
		// Leaving Pay: cancel in-flight client handlers (never pretend to cancel
		// server reconciliation - the token just drops the late response).
		if (prev === "pay" && s !== "pay") flow.cancelInFlight();
		// Re-entering Pay after editing details / cancelling reconnect: a fresh,
		// authoritative hydrate, never a stale in-memory state.
		if (s === "pay" && (prev === "details" || prev === "reconnect")) {
			reconcileMidFlightSignup();
		}
		// Same P1-8 lifecycle discipline for the pending auto-poll: stop it the
		// instant Pay is left (a late tick must not fire into a hidden step), and
		// (re)start it on entry if the machine is already sitting in UNKNOWN.
		restartPendingAutoPoll();
	}
);
onUnmounted(() => {
	if (cooldownTimer) clearInterval(cooldownTimer);
	// Stop the pending auto-poll: a sleeping tick otherwise fires a real check
	// into a torn-down component minutes after navigation away.
	pendingPollRun += 1;
	// Invalidate any in-flight client work so a late response cannot touch a
	// torn-down component (P1-8), and drop the checkout-return listeners.
	flow.cancelInFlight();
	window.removeEventListener("pageshow", onCheckoutPageShow);
	document.removeEventListener("visibilitychange", onCheckoutVisibility);
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
		state.erpnextInstalled = !!d.erpnext_installed;
	} catch (e) {
		/* no-op: keep the placeholders */
	}
}

// Returning from a full-page gateway checkout (plan 02 P0-2). A bfcache restore
// (pageshow with event.persisted) brings back the OLD instance with checkout_open
// frozen in memory and the sheet long gone; a tab regaining focus can do the same
// after a redirect. Gated on the external-nav marker so an ordinary tab-switch on
// a Razorpay modal (which sets no marker and is still on-screen) is never force
// -exited. hydrate() deliberately refuses to leave checkout_open, so this drives
// the machine's explicit, safe RETURNED_FROM_CHECKOUT exit + server reconcile.
function handleCheckoutReturn() {
	const marker = readExternalCheckoutNav();
	if (!marker) return;
	const inCheckout = pay.value.value === S.CHECKOUT_OPEN;
	// A marker from a previous attempt (or when no sheet is open) is stale: clear it
	// without driving a returnFromCheckout, so it can never drop a live confirm (X3).
	if (!shouldHonorCheckoutReturn({ marker, inCheckout, attemptId: pay.value.attemptId })) {
		clearExternalCheckoutNav();
		return;
	}
	clearExternalCheckoutNav();
	whileConfirmingReturn(() => flow.returnFromCheckout());
}
function onCheckoutPageShow(e) {
	// Only a bfcache restore needs handling here; a normal load re-mounts fresh
	// and hydrate() reads server truth on its own.
	if (e && e.persisted) handleCheckoutReturn();
}
function onCheckoutVisibility() {
	if (document.visibilityState === "visible") handleCheckoutReturn();
}

onMounted(async () => {
	// Did we just come back from the admin-hosted checkout? The pay page appends
	// `?pay=done|failed|pending` on its way out (jarvis_admin_v2's
	// billing/checkout/workspace.py owns that vocabulary). Read it BEFORE anything
	// else touches the URL, and strip it immediately so a later reload does not
	// re-apply a stale verdict.
	const checkoutReturn = readCheckoutOutcome();
	// Returning from the pay page: show the confirming screen from first paint.
	// Everything below this line that the customer would otherwise be watching is
	// real work - a providers fetch, an account prefill, then the reconcile round
	// trip (hydrate, then a readiness read) - and until that lands `state.step` is
	// still the default "intro". So a customer who paid ten seconds ago sat
	// watching the marketing tour while we worked out what had happened to their
	// money. A correction for that already existed at the end of this hook, but it
	// ran AFTER the awaits: it fixed where they landed, not what they watched.
	//
	// Set before the first await deliberately. Routing to "pay" here reaches the
	// same landing the correction did (landingStep leaves a resumed "pay" alone),
	// which is why that guard below is now a no-op rather than a repair. The flag
	// is cleared in the finally around the reconcile; nothing awaited in between
	// can reject (prefillAccount swallows its own errors), so it cannot strand.
	if (checkoutReturn) {
		state.step = "pay";
		confirmingReturn.value = true;
	}
	// A `done` OR `pending` return both prove the customer FINISHED the Razorpay flow;
	// only the outcome differs - `done` = payment confirmed, `pending` = authorized and
	// awaiting async confirmation. The admin pay page (billing/checkout/shell.py
	// outcomeFor) returns `pending` for exactly the PAYMENT_AUTHORIZED_PENDING_CONFIRM
	// auto-pay-mandate case this hold exists for (next_step "poll"/"contact_support"),
	// so gating on `done` alone missed the PRIMARY path. A `failed` return, or a bare
	// reload with no ?pay= (a checkout nobody finished), still falls to the recovery
	// card. See returnedFromCompletedCheckout's declaration.
	returnedFromCompletedCheckout.value =
		checkoutReturn === "done" || checkoutReturn === "pending";
	// Restore the namespaced local billing snapshot FIRST: restored values are
	// user-owned (local_restore), so the Company-defaults fetch prefillAccount
	// triggers can only fill fields the customer left blank.
	billing.restore();
	// The identity half of that snapshot (work email, company). Applied before
	// prefillAccount so what the CUSTOMER typed beats getAccountDefaults, which can
	// legitimately answer with the SITE ADMINISTRATOR's email - a different person.
	// Without this, returning from checkout showed the wrong email on the Details
	// step, or none at all.
	if (billing.identity.email) {
		state.email = billing.identity.email;
		state.identityFromUser = true;
	}
	if (billing.identity.company) {
		state.company = billing.identity.company;
		state.identityFromUser = true;
	}
	// Fired (not awaited) synchronously so providersLoading flips true on this same
	// tick, before the awaited prefill below — the discovery loading note must show
	// from first paint (X4), independent of prefill/company.
	loadPaymentProviders();
	// Best-effort terms-page link for the Details-step checkbox. Never blocks the
	// wizard and never throws into onMounted: a failure just leaves state.termsUrl
	// empty, which the template renders as plain unlinked "Terms & Conditions" text.
	getTermsUrl()
		.then((d) => {
			state.termsUrl = (d && d.url) || "";
		})
		.catch(() => {});
	await prefillAccount();
	// prefillAccount may set the default Company synchronously; the watcher fires,
	// but kick a fetch explicitly too in case the prefilled value equalled the
	// combo's initial value (no change event) — beginCompanyFetch is idempotent
	// for the same Company (it never clears same-Company erp_default values).
	if ((state.company || "").trim()) scheduleCompanyDefaults();
	window.addEventListener("pageshow", onCheckoutPageShow);
	document.addEventListener("visibilitychange", onCheckoutVisibility);
	// Clear any stale external-checkout marker BEFORE the first await (X3). Left
	// after `await reconcileMidFlightSignup()`, a visibilitychange/pageshow firing
	// during that await could honour a prior attempt's leftover marker. A fresh
	// mount is never mid-sheet (state is review until a sheet is opened), so this
	// only ever drops a genuinely stale marker; a bfcache restore does not re-run
	// onMounted, so a live restored checkout is untouched.
	if (pay.value.value !== S.CHECKOUT_OPEN) {
		clearExternalCheckoutNav();
	}
	try {
		const midFlightTruth = await reconcileMidFlightSignup();
		// FRESH-MOUNT RETURN HEAL. `checkoutReturn` only proves the pay page sent
		// the customer back top-level; the passive hydrate above only read admin's
		// last-known DB row (get_onboarding_state), never asked the gateway. A
		// fresh mount can never be CHECKOUT_OPEN (paymentMachine.js reinitializes to
		// REVIEW), so the in-memory RETURNED_FROM_CHECKOUT exit that already asks
		// the gateway (usePaymentFlow's returnFromCheckout / handleCheckoutReturn)
		// is unreachable here - this is the ONLY path that converges a fresh-mount
		// return. Mirrors BillingPage.vue's run-healer-once pattern: exactly one
		// active check, only when a mid-flight signup exists (nothing to check
		// otherwise - a day-one visitor's check_signup_payment_status call would
		// just spend a rate-limited read on an absent subscription row).
		//
		// No extra fencing needed beyond that: checkStatus() routes its answer
		// through the SAME applyContract every other answer takes, so the
		// attempt/generation fence and the PAID floor already guarantee a stale
		// answer here can never repaint a newer state and a paid answer is never
		// undone (paymentMachine.js applyContract).
		if (checkoutReturn && midFlightTruth && !midFlightTruth.notStarted) {
			await flow.checkStatus();
		}
	} finally {
		confirmingReturn.value = false;
	}
	// Resume an apply that was in flight when the page was last closed/reloaded: follow
	// the SAME operation rather than showing an editable form over a running one (P1-05).
	await maybeResumeConnect();
	// AFTER the resumes: they set state.step from persisted signup state, and the
	// customer's explicit intent has to win over that. Never overrides a finished
	// signup - somebody already paid and provisioned is not reconnecting.
	const intent = hasReconnectIntent(window.location.search);
	const landing = landingStep({
		intent,
		resumedStep: state.step,
		terminal: isTerminalForPayment(pay.value.value),
	});
	state.reconnectIntent = intent && landing === "details";
	state.step = landing;
	// Coming back from checkout must NEVER land on the intro tour. reconcile above
	// already routes from server truth in the normal case, but there are real races
	// where it cannot: the control plane may not have absorbed the gateway's answer
	// yet, so a customer who genuinely paid ten seconds ago can hydrate as "nothing
	// started" and be shown the marketing tour as though they had never signed up.
	// The pay page told us where they came from, so honour that.
	if (checkoutReturn && state.step === "intro") {
		state.step = "pay";
	}
	// Prefetch the plan catalog behind the intro tour so the Plan step rarely
	// first-paints in its loading state. Reconciled resumes land past "plan"
	// and skip it (the step-entry watch still covers every other path).
	if (state.step === "intro" && !state.plans.length && !state.plansLoading) loadPlansSafe();
});

// Tear down the single controller (kills its timers; the {aborted:true} rejection is
// swallowed by followDescriptor) and the retry countdown when the wizard unmounts.
onUnmounted(() => {
	if (opController) opController.abort();
	stopRetryCountdown();
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
/* `--accent` is defined in NEITHER palette, so both of these fell through to a
   raw blue that exists nowhere in the token set - and design.md §2.6 asks for a
   neutral ring, not a coloured glow. Focus is the near-black CTA edge plus a
   soft neutral ring, which is what every other focusable control here does. */
.ob-code:focus {
	border-color: var(--cta);
	box-shadow: 0 0 0 3px var(--surface-2);
}
.ob-code-note {
	margin: 14px 0 0;
	max-width: 420px;
	text-align: center;
	font-size: 13px;
	line-height: 1.55;
	color: var(--text-2);
}
/* ONE definition. There were two: this rule and a second, later block that
   repainted it 12.5px --text-3 gray. The later one won, so every inline link on
   these steps - including the "Contact support" handoff jarvis#709 added at the
   readiness ceiling, and the provider "Retry" - rendered as small gray text that
   did not look clickable. design.md §1.1 reserves --link for links and §3.1 says
   links look like links; the old `--accent` fallback was also an undefined var
   resolving to a raw blue outside the token set. */
.ob-link {
	font: inherit;
	font-family: inherit;
	color: var(--link);
	background: none;
	border: 0;
	padding: 0;
	cursor: pointer;
	text-decoration: underline;
	text-underline-offset: 2px;
}
.ob-link:hover {
	text-decoration-thickness: 2px;
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
/* The connect wait's expectation-setting note (jarvis onboarding time note):
   needs the extra class on the specificity to beat .ob-head p above on rule
   weight, not source order, since it sits right after the subtitle in the
   same .ob-head block and both are <p> tags. Smaller and greyer than the
   subtitle by design - a secondary line, not a second headline. */
.ob-head p.ob-head-note {
	margin-top: 6px;
	font-size: 12.5px;
	color: var(--text-3);
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
/* ---- staged wait progress bar (jarvis#726, waitPhases.phaseProgress) ----
   Layout wrapper only - the bar itself, including the indeterminate pulse,
   is StepProgress.vue's variant="bar" (design.md §4.3, one shared component
   for every stepped indicator in the app; 2026-08-16 redesign replaced its
   per-segment columns with one continuous fill).

   Width (jarvis wait-phases-horizontal): a live run called the old 420px cap
   "very congested". 75% of the card reads roomy without turning into an
   unreadably long line on a wide monitor - 640px caps that, chosen so the
   longest phase label ("Applying your AI configuration") still gets a
   sensible column width at 3-up rather than the columns ballooning past what
   the text needs. `.ob-phases` and `.ob-phase-detail` below share the exact
   same width/gap so the phase columns line up under the bar and the detail
   line reads as belonging to the same block. */
.ob-progress {
	margin: 0 auto;
	width: 75%;
	max-width: 640px;
}

/* ---- staged wait phases (waitPhases.js), PROVISIONING wait only --------
   One column per phase, side by side under the bar, so the whole block
   reads as one horizontal progression rather than a bar with an unrelated
   list under it (jarvis wait-phases-horizontal). The columns line up under
   the bar because both share the same 75%/640px width and 8px gap, not
   because the bar itself is still segmented - since the 2026-08-16 redesign
   the bar above is one continuous fill (StepProgress.vue variant="bar").
   jarvis#763 rejected per-step labels on the bar itself BECAUSE these
   columns already carried the words; the CONNECT wait's own six-step bar
   went through two more rounds after that (the 2026-08-14 labeled-segment
   layout, then the 2026-08-16 caption-only bar) and only the provisioning
   wait still renders this column list.

   The MODIFIER on each column is the honesty contract, not decoration:
   `active` is only ever set from an observation, `unknown` means a poll
   answered with nothing (or with the absence of a verdict) and must never
   look like progress, and `waiting` says nothing at all about a phase that
   has not started. Colour carries no status here beyond the completed check -
   the words do that.

   Below 720px there is no room for three icon+label columns without wrapping
   into a cramped, unreadable tower, so the media query at the bottom of this
   file reverts to the original stacked single column. */
.ob-phases {
	list-style: none;
	margin: 0 auto;
	padding: 0;
	width: 75%;
	max-width: 640px;
	display: flex;
	flex-direction: row;
	align-items: stretch;
	gap: 8px;
}
.ob-phase {
	flex: 1 1 0;
	min-width: 0;
	display: flex;
	align-items: flex-start;
	gap: 10px;
	padding: 7px 8px;
	border-radius: 8px;
}
.ob-phase-ico {
	width: 20px;
	height: 20px;
	flex: none;
	display: grid;
	place-items: center;
}
.ob-phase-txt {
	min-width: 0;
}
.ob-phase-label {
	display: block;
	font-size: 13px;
	line-height: 1.5;
}
/* Hoisted OUT of the phase column (jarvis wait-phases-horizontal): the active
   phase's `detail` is admin's own full sentence (jarvis#752/#754, e.g. "Your
   OpenAI account has reached its usage limit. It resets in 2 hours."), too
   long for a column a third of this block's width without wrecking the row.
   Only one phase is ever active, so there is only ever one detail: it renders
   once, full width, centered below every column. Its own `role="status"`
   keeps detail changes announced now that they are no longer inside the
   active column's status region. */
.ob-phase-detail {
	display: block;
	width: 75%;
	max-width: 640px;
	margin: 6px auto 0;
	padding: 0 8px;
	font-size: 12px;
	line-height: 1.5;
	color: var(--text-3);
	text-align: center;
	box-sizing: border-box;
}
/* The connect bar's one-line explanation of the CURRENT step (2026-08-14
   redesign): the sentence the phase columns used to carry. Slightly stronger
   than .ob-phase-detail below it, which stays reserved for admin's own detail
   sentence and the preflight notice. */
.ob-step-explain {
	display: block;
	width: 75%;
	max-width: 640px;
	margin: 10px auto 0;
	padding: 0 8px;
	font-size: 13px;
	line-height: 1.5;
	color: var(--text-2);
	font-weight: 500;
	text-align: center;
	box-sizing: border-box;
}
.ob-phase--done .ob-phase-label {
	color: var(--text-2);
}
.ob-phase--done .ob-phase-ico {
	color: var(--green);
}
.ob-phase--active,
.ob-phase--unknown {
	background: var(--surface-1);
}
.ob-phase--active .ob-phase-label,
.ob-phase--unknown .ob-phase-label {
	color: var(--text);
	font-weight: 500;
}
.ob-phase--active .ob-phase-ico {
	color: var(--text-2);
}
.ob-phase--unknown .ob-phase-ico {
	color: var(--text-3);
}
.ob-phase--waiting .ob-phase-label {
	color: var(--text-3);
}
.ob-phase-dot {
	width: 7px;
	height: 7px;
	border-radius: 50%;
	background: var(--surface-3);
	border: 1px solid var(--border-2);
	display: block;
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
/* Narrow window (jarvis wait-phases-horizontal): three icon+label columns
   plus the bar above them do not fit a phone-width card without the labels
   wrapping into a cramped tower, so below 720px the phase columns revert to
   the original single-column stack and the block itself uses the full
   available width instead of 75% of an already-small card. 720, not the
   block's own 640px cap: right at that cap the card is not yet wide enough
   for 75% of it to give each column real breathing room (the ob-body's own
   padding and the card's max-width still leave the card under ~700px in that
   band), so the fallback range is deliberately wider than the point the cap
   stops mattering. */
@media (max-width: 720px) {
	.ob-progress,
	.ob-phases,
	.ob-phase-detail,
	.ob-step-explain {
		width: 100%;
	}
	.ob-phases {
		flex-direction: column;
		gap: 2px;
	}
	.ob-phase {
		flex: none;
	}
}
@media (prefers-reduced-motion: reduce) {
	.ob-screen {
		animation: none;
	}
	.ob-details-form :deep(.jvc-field) {
		transition: none;
	}
	/* StepProgress.vue owns its own reduced-motion fallback for the
	   indeterminate fill/segment pulse. */
}
</style>
