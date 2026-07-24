<template>
	<!-- Reusable LLM-pool editor. Renders the 3-mode SETUP UI (Quick | Preset |
       Custom) over the unified proxy-pool model and persists via saveLlmPool.
       Self-loads its config on mount (seeded through seedRowsFromConfig into the
       canonical camelCase row shape). Expects an ancestor to supply the theme
       CSS vars (--surface, --text, …); uses only tokens, no hard-coded colors.
       Consumers: AiView (manage tab) now, AccountView + onboarding later. -->
	<div class="jv-llm-editor" style="font-family: inherit; color: var(--text)">
		<div v-if="err" style="color: var(--red); font-size: 13px; margin-bottom: 12px">
			{{ err }}
			<button type="button" class="jv-mon-retry" @click="load">Retry</button>
		</div>

		<!-- ============================================================
         UNIFIED FAILOVER LIST + CONFIG SECTION (!singleMode only - the
         Account/Settings editor). Onboarding never reaches this branch
         (singleMode forces llmMode==='quick' below). Phase 1: read list
         only; config section arrives in phase 2. ============================================================ -->
		<section v-if="!singleMode" style="margin-bottom: 18px">
			<!-- No section heading and no explainer here: the dialog already titles this
           pane ("AI models" / "The AI connection that powers Jarvis."), so an
           uppercase "AI MODELS" repeated below it was pure duplication. The failover
           behaviour is surfaced where it's actionable instead: on the "No backup yet"
           card, which self-hides once a second model exists.
           The badge stays - it only appears for a real multi-model failover pool. -->
			<div
				v-if="badgeLabel"
				style="
					display: flex;
					align-items: center;
					gap: 10px;
					margin-bottom: 10px;
					flex-wrap: wrap;
				"
			>
				<span
					style="
						font-size: 12px;
						font-weight: 600;
						padding: 4px 11px;
						border-radius: 20px;
						background: var(--green-bg);
						color: var(--green);
					"
				>
					{{ badgeLabel }}
				</span>
			</div>

			<!-- Legacy DIRECT chat-subscription (flat-field OAuth, no proxy) - not
           part of rows.value/the failover pool, so no order badge/reorder. -->
			<div v-if="showDirectRow" class="jv-flist-row">
				<span class="jv-flist-chip">Direct</span>
				<span class="jv-flist-model">{{
					directStatus.model || directStatus.provider || "Chat subscription"
				}}</span>
				<span
					v-if="directStatus.account_email"
					style="font-size: 11px; color: var(--text-3)"
					>{{ directStatus.account_email }}</span
				>
				<span class="jv-pool-dot jv-pool-dot--ok" aria-hidden="true"></span>
				<span class="jv-flist-acts">
					<button
						v-if="editable"
						@click="directPanelOpen = !directPanelOpen"
						class="jv-btn jv-btn--sm jv-btn--ghost"
					>
						{{ directPanelOpen ? "Close" : "Reconnect" }}
					</button>
					<button
						v-if="editable"
						@click="removeDirect"
						class="jv-btn jv-btn--sm jv-btn--ghost jv-pool-disc"
					>
						Remove
					</button>
				</span>
			</div>
			<div v-if="showDirectRow && directPanelOpen" class="jv-cfgpanel">
				<DirectSubscriptionCard
					:status="directStatus"
					:editable="editable"
					@reauthorized="onDirectCardChanged"
					@disconnected="onDirectCardChanged"
				/>
			</div>

			<div
				v-if="!rows.length && !showDirectRow"
				style="font-size: 13px; color: var(--text-3); padding: 8px 0"
			>
				No models yet. Add one below.
			</div>

			<div v-for="(row, i) in rows" :key="row._uid ?? i" class="jv-flist-row">
				<span class="jv-pool-badge">{{ i + 1 }}</span>
				<ProviderLogo :provider="row.provider" :upstream="row.upstream" :size="18" />
				<span class="jv-flist-chip">{{ sourceChip(row) }}</span>
				<span class="jv-flist-model" :class="{ 'jv-flist-model--unset': !row.model }">{{
					rowModelLabel(row)
				}}</span>
				<span
					v-if="row.credentialType !== 'subscription' && row.hasKey"
					style="font-size: 11px; color: var(--text-3)"
					>key set</span
				>
				<span
					class="jv-pool-dot"
					:class="'jv-pool-dot--' + accountHealth(row).level"
					aria-hidden="true"
				></span>
				<span
					v-if="accountHealth(row).label"
					class="jv-pool-acct-health"
					:class="'jv-pool-acct-health--' + accountHealth(row).level"
					:title="accountHealth(row).title"
					>{{ accountHealth(row).label }}</span
				>
				<!-- Reorder + [Edit][Reconnect|Replace key][Remove], always right-aligned.
             All stay LIVE while the config panel is open. The panel used to track its
             target row by ARRAY INDEX, so reordering or removing underneath it silently
             repointed it at a different row (open Edit on row 2, start OAuth, reorder
             row 1 while the sign-in tab is up -- the pasted callback attached the new
             account to the OTHER model and auto-saved it). Disabling the buttons closed
             that hole but dead-ended the customer: a pool whose only invalid row is a
             blank seeded one could not be saved OR emptied while the panel was open.
             The panel now tracks its row by IDENTITY (panel.uid -> row._uid), so the
             array can be mutated freely and the panel always follows its own row. -->
				<span class="jv-flist-acts">
					<button
						@click="move(i, -1)"
						:disabled="!editable || i === 0"
						title="Up"
						class="jv-pool-iconbtn"
					>
						<svg
							viewBox="0 0 24 24"
							width="14"
							height="14"
							fill="none"
							stroke="currentColor"
							stroke-width="1.8"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M18 15l-6-6-6 6" />
						</svg>
					</button>
					<button
						@click="move(i, 1)"
						:disabled="!editable || i === rows.length - 1"
						title="Down"
						class="jv-pool-iconbtn"
					>
						<svg
							viewBox="0 0 24 24"
							width="14"
							height="14"
							fill="none"
							stroke="currentColor"
							stroke-width="1.8"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M6 9l6 6 6-6" />
						</svg>
					</button>
					<button
						v-if="editable"
						@click="openEdit(i)"
						class="jv-btn jv-btn--sm jv-btn--ghost"
					>
						Edit
					</button>
					<button
						v-if="editable && row.credentialType === 'subscription'"
						@click="quickReconnect(i)"
						class="jv-btn jv-btn--sm jv-btn--ghost"
					>
						Reconnect
					</button>
					<button
						v-else-if="editable"
						@click="openEdit(i)"
						class="jv-btn jv-btn--sm jv-btn--ghost"
					>
						Replace key
					</button>
					<button
						v-if="editable"
						@click="remove(i)"
						class="jv-btn jv-btn--sm jv-btn--ghost jv-pool-disc"
					>
						Remove
					</button>
				</span>
			</div>

			<!-- A real .jv-btn, not a dashed row: this is a LIST ACTION that opens the
           config panel, so it belongs to the same button system as the row actions
           (Edit / Reconnect / Remove) and Save. The dashed treatment is reserved for
           an EMPTY SLOT the content will fill -- which is what "+ Connect account"
           is, inside the panel, where the account itself then appears. Ghost, not
           primary: Save configuration stays the pane's single primary action. -->
			<button
				v-if="editable && !panel.open"
				@click="openAdd"
				class="jv-btn jv-btn--primary jv-flist-addbtn jv-flist-addbtn--end"
			>
				<svg
					viewBox="0 0 24 24"
					width="15"
					height="15"
					fill="none"
					stroke="currentColor"
					stroke-width="1.9"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M12 5v14M5 12h14" />
				</svg>
				Add a model
			</button>

			<!-- A single model has nothing to fall back to. Name the consequence instead
           of leaving the customer to infer it from "tried in order". -->
			<div
				v-if="editable && !panel.open && rows.length === 1 && !showDirectRow"
				class="jv-flist-hint"
			>
				<svg
					viewBox="0 0 24 24"
					width="15"
					height="15"
					fill="none"
					stroke="currentColor"
					stroke-width="1.7"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<circle cx="12" cy="12" r="9" />
					<path d="M12 16v-4M12 8h.01" />
				</svg>
				<span
					><b>No backup yet.</b> If this model fails or hits its limit, chat stops. Add a
					second one and {{ agentName }} switches over automatically.</span
				>
			</div>

			<!-- Master-detail config section: Add/Edit a single row, or (add-mode
           only) apply a preset that replaces the whole pool. Field markup +
           connect flow are reused verbatim from the account editor's former
           per-row layout; only the panel container is new. -->
			<div v-if="panel.open" class="jv-cfgpanel">
				<div class="jv-cfgpanel-head">
					<div class="jv-cfgpanel-title">
						{{ panel.mode === "add" ? "Add a model" : "Edit model" }}
					</div>
				</div>

				<div
					class="jv-pool-segct"
					role="group"
					aria-label="Source"
					style="margin-bottom: 12px"
				>
					<button
						type="button"
						class="jv-pool-segbtn"
						:class="{ on: panel.source === 'subscription' }"
						:disabled="!editable"
						@click="setPanelSource('subscription')"
					>
						Chat subscription
					</button>
					<button
						type="button"
						class="jv-pool-segbtn"
						:class="{ on: panel.source === 'api_key' }"
						:disabled="!editable"
						@click="setPanelSource('api_key')"
					>
						API key
					</button>
					<!-- Presets are NOT shipping yet: shown but disabled, so the capability is
               discoverable without being clickable. Keep setPanelSource('preset')
               and the preset branch below intact - re-enabling is just dropping the
               `disabled` and the tag. -->
					<button
						v-if="panel.mode === 'add'"
						type="button"
						class="jv-pool-segbtn jv-pool-segbtn--soon"
						disabled
						title="Coming soon"
					>
						From a preset<span class="jv-soon">Soon</span>
					</button>
				</div>

				<!-- API-key source -->
				<div v-if="panel.source === 'api_key' && panelRow">
					<!-- 2x2 grid, not four fields crammed across one row: the flex ratios
               (1 / 1.5 / 1.5 / 1.5) produced four different widths and read as
               noise. Provider first, since it decides the model suggestions. -->
					<div class="jv-cfg-grid">
						<!-- JvCombo, not a native <select>/<datalist>: the OS renders those with
                 its own popup (dark, system-styled), which is why this panel looked
                 nothing like onboarding. JvCombo is the app's dropdown and is already
                 what onboarding uses, so both surfaces now render identically. -->
						<div class="jv-pool-field">
							<label class="jv-pool-lab">Provider</label>
							<JvCombo
								:model-value="panelRow.provider"
								:options="providerOptions"
								:editable="editable"
								placeholder="Provider"
								@update:model-value="(v) => onProviderChange(panelRow, v)"
							>
								<template #option="{ option }"
									><span
										style="display: inline-flex; align-items: center; gap: 8px"
										><ProviderLogo :provider="option.value" :size="16" />{{
											option.label
										}}</span
									></template
								>
								<template #selected="{ label, placeholder }"
									><span
										style="display: inline-flex; align-items: center; gap: 8px"
										><ProviderLogo
											v-if="label"
											:provider="label"
											:size="16"
										/>{{ label || placeholder }}</span
									></template
								>
							</JvCombo>
						</div>
						<div class="jv-pool-field">
							<label class="jv-pool-lab">Model</label>
							<JvCombo
								:model-value="panelRow.model"
								:options="modelSuggestionsForProvider(panelRow.provider)"
								:editable="editable"
								allow-custom
								placeholder="Model ID (e.g. gpt-4o)"
								@update:model-value="
									(v) => {
										panelRow.model = v;
									}
								"
							/>
						</div>
						<div class="jv-pool-field">
							<label class="jv-pool-lab"
								>API key<span
									v-if="isLocalProviderRow(panelRow)"
									class="jv-pool-opt"
								>
									(optional)</span
								></label
							>
							<input
								v-model="panelRow.apiKey"
								:disabled="!editable"
								type="password"
								:placeholder="
									panelRow.hasKey
										? 'key set, re-enter to change'
										: isLocalProviderRow(panelRow)
										? 'Not required for local providers'
										: 'API key'
								"
								class="jv-cfg-inp"
							/>
						</div>
						<div class="jv-pool-field">
							<label class="jv-pool-lab"
								>Base URL <span class="jv-pool-opt">(optional)</span></label
							>
							<input
								v-model="panelRow.baseUrl"
								:disabled="!editable"
								placeholder="Base URL (OpenAI-compatible)"
								class="jv-cfg-inp"
							/>
						</div>
					</div>

					<!-- Pre-save "Test": a live, side-effect-free 1-token request straight from this
               bench to the provider using whatever is typed above - never saved, never
               touches the fleet/container (jarvis.llm_key_probe.test_llm_api_key). Motivated
               by a real GLM/Z.ai case: a valid key on a zero-balance account saved cleanly and
               only failed AFTER save with a bare "Not working" - this lets the customer catch
               that (and see the provider's OWN error) before they ever click Save. -->
					<div
						style="
							display: flex;
							align-items: center;
							gap: 10px;
							margin-top: 11px;
							flex-wrap: wrap;
						"
					>
						<button
							type="button"
							class="jv-btn jv-btn--sm jv-btn--ghost"
							:disabled="!editable || panel.testing || !!testBlockedReason(panelRow)"
							:title="testBlockedReason(panelRow) || testButtonHint(panelRow)"
							@click="testApiKeyRow(panelRow)"
						>
							{{ panel.testing ? "Testing…" : "Test" }}
						</button>
						<span
							v-if="testBlockedReason(panelRow)"
							class="jv-pool-opt"
							style="font-size: 11.5px"
							>{{ testBlockedReason(panelRow) }}</span
						>
						<span
							v-else-if="isLocalProviderRow(panelRow)"
							class="jv-pool-opt"
							style="font-size: 11.5px"
							>Local endpoint - only verifiable from inside your Jarvis
							container</span
						>
					</div>
					<div
						v-if="panel.testResult"
						class="jv-status"
						:class="panel.testResult.ok ? 'jv-status-ok' : 'jv-status-bad'"
						style="margin-top: 10px"
					>
						<span class="jv-status-ic">
							<svg
								v-if="panel.testResult.ok"
								width="14"
								height="14"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2.4"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M20 6 9 17l-5-5" />
							</svg>
							<svg
								v-else
								width="14"
								height="14"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2.4"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M18 6 6 18M6 6l12 12" />
							</svg>
						</span>
						<span class="jv-status-tx"
							><b>{{ panel.testResult.ok ? "Key works." : "Test failed." }}</b>
							{{ panel.testResult.message }}</span
						>
					</div>
					<div
						v-if="panel.testResult && panel.testResult.caveat"
						style="font-size: 11px; color: var(--text-3); margin-top: 5px"
					>
						{{ panel.testResult.caveat }}
					</div>

					<!-- Resilient-by-default (API KEYS ONLY): expands this provider into
               its full single-vendor failover chain on close, sharing the
               same key. Add-mode only. -->
					<label
						v-if="panel.mode === 'add'"
						style="
							display: flex;
							align-items: center;
							gap: 8px;
							margin-top: 11px;
							font-size: 13px;
							color: var(--text-2);
							cursor: pointer;
						"
					>
						<button
							type="button"
							class="jv-switch"
							:class="{ on: panel.addBackups }"
							:disabled="!editable"
							role="switch"
							:aria-checked="String(panel.addBackups)"
							@click="panel.addBackups = !panel.addBackups"
						>
							<span class="jv-switch-knob"></span>
						</button>
						Add backup models automatically (recommended)
					</label>
				</div>

				<!-- Chat-subscription source -->
				<div v-else-if="panel.source === 'subscription' && panelRow">
					<!-- Provider is the ONLY field for a chat subscription. There is no model
               picker: a plan grants you its model, so asking a customer to type a
               model id was busywork and an easy way to enter an invalid one. The id
               is derived from the provider (setCredType / onUpstreamChange), which
               validatePool + save still require. Onboarding already worked this way;
               the settings editor now matches it.
               Account rotation is likewise not exposed - it only matters once one
               provider has several accounts, and defaults to "sticky" in the schema. -->
					<div class="jv-cfg-grid" style="margin-bottom: 10px">
						<!-- JvCombo works in display LABELS, but a row stores `upstream` as the
                 VALUE ("openai"/"google") that the pool spec requires - hence the
                 label<->value bridge, rather than leaking "OpenAI" into the spec. -->
						<div class="jv-pool-field">
							<label class="jv-pool-lab">Provider</label>
							<!-- The same-value guard is LOAD-BEARING. JvCombo.choose() emits
                   update:model-value unconditionally, even when you re-pick the option
                   that is already selected -- and onUpstreamChange() drops every
                   connected account (an OAuth account is authorized against ONE
                   provider). Without this, clicking "OpenAI" on a row already set to
                   OpenAI silently DISCONNECTS a working subscription. The onboarding
                   combo below carries the same guard for the same reason. -->
							<JvCombo
								:model-value="upstreamLabelOf(panelRow.upstream)"
								:options="upstreamLabels"
								:editable="editable"
								placeholder="Provider"
								@update:model-value="
									(v) => {
										const nv = upstreamValueOf(v);
										if (nv === panelRow.upstream) return;
										panelRow.upstream = nv;
										onUpstreamChange(panelRow);
									}
								"
							/>
						</div>
					</div>

					<!-- Connect account: EDIT-mode re-entry only. In add mode the two-step sign-in
               renders directly (see its v-if below), so a fresh "Add a model" never shows
               this button - clicking "Connect account" and THEN "Open sign-in" was a
               redundant double-click for one intent. It reappears only in the EDIT panel
               when a row's last account was disconnected (removeAccount leaves _connect
               closed), giving a neutral re-entry point rather than auto-popping OAuth. -->
					<button
						v-if="
							editable &&
							panel.mode !== 'add' &&
							!(panelRow._connect && panelRow._connect.open) &&
							!(panelRow.accounts && panelRow.accounts.length)
						"
						@click="openConnectPanel(panelRow)"
						class="jv-btn jv-btn--primary jv-flist-addbtn"
					>
						<svg
							viewBox="0 0 24 24"
							width="15"
							height="15"
							fill="none"
							stroke="currentColor"
							stroke-width="1.9"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M12 5v14M5 12h14" />
						</svg>
						Connect account
					</button>

					<!-- Connected accounts (markup reused verbatim from the former
               per-row layout). -->
					<div
						v-if="panelRow.accounts && panelRow.accounts.length"
						class="jv-pool-accts"
					>
						<div class="jv-pool-lab">
							Connected accounts ({{ panelRow.accounts.length }})
						</div>
						<div class="jv-pool-acctlist">
							<div
								v-for="(a, ai) in panelRow.accounts"
								:key="a.account_ref || ai"
								class="jv-pool-acctchip"
							>
								<span class="jv-pool-avatar">{{
									(accountLabel(a) || "?").charAt(0).toUpperCase()
								}}</span>
								<span class="jv-pool-accttx">{{ accountLabel(a) }}</span>
								<span
									class="jv-pool-dot"
									:class="'jv-pool-dot--' + accountHealth(panelRow).level"
									aria-hidden="true"
								></span>
								<span
									v-if="accountHealth(panelRow).label"
									class="jv-pool-acct-health"
									:class="
										'jv-pool-acct-health--' + accountHealth(panelRow).level
									"
									:title="accountHealth(panelRow).title"
									>{{ accountHealth(panelRow).label }}</span
								>
								<span class="jv-pool-acctacts">
									<button
										v-if="editable"
										class="jv-btn jv-btn--sm jv-btn--ghost"
										@click="openConnectPanel(panelRow, ai)"
										title="Re-authorize to mint fresh tokens"
									>
										Reconnect
									</button>
									<button
										v-if="editable"
										class="jv-btn jv-btn--sm jv-btn--ghost jv-pool-disc"
										@click="removeAccount(panelRow, ai)"
									>
										Disconnect
									</button>
								</span>
							</div>
							<!-- Ghost, not primary: an account already exists, so adding a SECOND is
                   optional. It also sits beside Reconnect / Disconnect, which are ghosts.
                   (Primary is reserved for the required next step -- Connect account when
                   there is none, and Save configuration.) -->
							<button
								v-if="editable && !(panelRow._connect && panelRow._connect.open)"
								@click="openConnectPanel(panelRow)"
								class="jv-btn jv-btn--sm jv-btn--ghost"
							>
								+ Add account
							</button>
						</div>
					</div>
					<!-- ("No accounts connected yet." removed: the Connect account button in the
               grid above already says the account is missing; the sentence only
               restated it.) -->

					<!-- OAuth connect: the SAME two-step spine onboarding renders (jv-csteps).
               Shown DIRECTLY for a fresh "Add a model" (panel.mode==='add' with no
               account) so there is no "Connect account" pre-step - matching onboarding.
               In the EDIT panel it appears only once opened via "+ Add account" or
               "Reconnect" (_connect.open), so disconnecting a row's last account drops
               back to the neutral button above rather than auto-popping OAuth. Step 1's
               button starts OAuth inside its own click (preserving the user gesture /
               popup-blocker fix); step 2 stays pending until step 1 mints the authorize
               URL, since a URL pasted before sign-in has no nonce and finishConnect no-ops. -->
					<div
						v-if="
							panelRow._connect &&
							(panelRow._connect.open ||
								(panel.mode === 'add' &&
									!(panelRow.accounts && panelRow.accounts.length)))
						"
						class="jv-csteps"
					>
						<!-- DEVICE-CODE (Kimi): show the code + verification link, poll for approval. -->
						<template v-if="panelRow._connect.deviceFlow">
							<div class="jv-cstep">
								<div class="jv-cnum">1</div>
								<div class="jv-cbody">
									<div class="jv-ctit">
										Sign in with {{ upstreamLabelOf(panelRow.upstream) }}
									</div>
									<div class="jv-cdesc">
										Open the verification page, enter the code, and approve
										access. This panel updates automatically.
									</div>
									<div class="jv-crow" style="margin-top: 8px">
										<a
											v-if="panelRow._connect.verificationUri"
											:href="panelRow._connect.verificationUri"
											target="_blank"
											rel="noopener noreferrer"
											class="jv-cbtn jv-cbtn-primary"
											>Open verification page ↗</a
										>
									</div>
									<div
										v-if="panelRow._connect.userCode"
										class="jv-cdesc"
										style="margin-top: 10px"
									>
										Code:
										<code
											style="
												font-size: 17px;
												letter-spacing: 2px;
												font-weight: 700;
											"
											>{{ panelRow._connect.userCode }}</code
										>
									</div>
									<div
										v-if="panelRow._connect.polling"
										class="jv-cdesc"
										style="margin-top: 8px"
									>
										Waiting for approval…
									</div>
								</div>
							</div>
							<div v-if="panelRow._connect.error" class="jv-cn-err">
								{{ panelRow._connect.error }}
							</div>
							<div class="jv-cn-acts">
								<button
									@click="closeConnect(panelRow)"
									class="jv-btn jv-btn--ghost"
								>
									Cancel
								</button>
							</div>
						</template>
						<!-- PASTE-BACK (OpenAI/Google/xAI): open sign-in, paste the callback URL. -->
						<template v-else>
							<div class="jv-cstep">
								<div class="jv-cnum">1</div>
								<div class="jv-cbody">
									<div class="jv-chead">
										<div class="jv-ctit">
											Sign in with {{ upstreamLabelOf(panelRow.upstream) }}
										</div>
										<div class="jv-crow">
											<a
												v-if="panelRow._connect.authorizeUrl"
												:href="panelRow._connect.authorizeUrl"
												target="_blank"
												rel="noopener noreferrer"
												class="jv-cbtn jv-cbtn-primary"
												>Open sign-in ↗</a
											>
											<button
												v-else
												type="button"
												class="jv-cbtn jv-cbtn-primary"
												:disabled="!editable || panelRow._connect.loading"
												@click="
													startConnect(
														panelRow,
														panelRow._connect.reconnectIdx
													)
												"
											>
												{{
													panelRow._connect.loading
														? "Starting sign-in…"
														: "Open sign-in ↗"
												}}
											</button>
											<!-- Always offered, never gated behind "Open sign-in": signing in on
                           another device needs the URL WITHOUT opening a tab here. -->
											<button
												type="button"
												class="jv-cbtn jv-cbtn-ghost"
												:disabled="
													panelRow._connect.loading ||
													(!editable && !panelRow._connect.authorizeUrl)
												"
												@click="copySigninLink(panelRow)"
											>
												{{
													panelRow._connect.copied
														? "Copied ✓"
														: "Copy link"
												}}
											</button>
										</div>
									</div>
									<div class="jv-cdesc">
										Opens {{ upstreamLabelOf(panelRow.upstream) }} in a new
										tab. Approve access, then come back here.
									</div>
								</div>
							</div>
							<div
								class="jv-cstep"
								:class="{ 'jv-pending': !panelRow._connect.authorizeUrl }"
							>
								<div class="jv-cnum">2</div>
								<div class="jv-cbody">
									<div class="jv-ctit">{{ pasteTitle(panelRow.upstream) }}</div>
									<div class="jv-callout">
										<svg
											width="15"
											height="15"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.9"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<circle cx="12" cy="12" r="9" />
											<path d="M12 8v5M12 16h.01" />
										</svg>
										<p v-if="isCodeOnlyPaste(panelRow.upstream)">
											After you approve,
											{{ upstreamLabelOf(panelRow.upstream) }} shows you an
											<b>authorization code</b>. Copy that code and paste it
											below.
										</p>
										<p v-else>
											After you approve, the browser shows a
											<b>&ldquo;This site can&rsquo;t be reached&rdquo;</b>
											page. That&rsquo;s expected: copy the
											<b>full URL from the address bar</b>
											(<kbd>⌘/Ctrl</kbd>+<kbd>L</kbd>, then
											<kbd>⌘/Ctrl</kbd>+<kbd>C</kbd>) and paste it below.
										</p>
									</div>
									<input
										v-model="panelRow._connect.pastedUrl"
										class="jv-paste"
										:disabled="!editable || !panelRow._connect.authorizeUrl"
										:placeholder="
											pastePlaceholder(
												panelRow.upstream,
												panelRow._connect.authorizeUrl
											)
										"
										@keydown.enter="finishConnect(panelRow)"
									/>
								</div>
							</div>
							<div v-if="panelRow._connect.error" class="jv-cn-err">
								{{ panelRow._connect.error }}
							</div>
							<!-- Actions right-aligned, like every other confirm action in this pane.
                 The inner Cancel is EDIT-mode only: there it calls closeConnect to
                 collapse the steps back to the account list. In add mode the steps ARE
                 the panel (they render directly from panel.mode==='add'), so closeConnect
                 can't hide them - it would leave an inert "Cancel". Abandoning a fresh add
                 is the panel's own Cancel/Close below; "Connect" stays to submit. -->
							<div class="jv-cn-acts">
								<button
									v-if="panel.mode !== 'add'"
									@click="closeConnect(panelRow)"
									class="jv-btn jv-btn--ghost"
								>
									Cancel
								</button>
								<button
									@click="finishConnect(panelRow)"
									:disabled="
										panelRow._connect.loading ||
										!panelRow._connect.authorizeUrl ||
										!(panelRow._connect.pastedUrl || '').trim()
									"
									class="jv-btn jv-btn--primary"
								>
									{{ panelRow._connect.loading ? "Connecting…" : "Connect" }}
								</button>
							</div>
						</template>
					</div>
				</div>

				<!-- From a preset (add-mode only) - picking a card replaces the whole
             pool, same as the account editor's former Preset tab, just
             relocated here (selectPreset/missingVendors/saveBlocked reused
             verbatim). -->
				<div v-else-if="panel.source === 'preset'">
					<p
						v-if="!catalog.length"
						style="font-size: 14px; color: var(--text-3); margin: 0 0 12px"
					>
						Couldn't load presets. Use <b>Chat subscription</b> or <b>API key</b>.
					</p>
					<div v-else style="max-height: 360px; overflow-y: auto; padding-right: 4px">
						<div v-if="singleVendorPresets.length" style="margin-bottom: 10px">
							<div
								style="
									font-size: 13px;
									font-weight: 600;
									color: var(--text-2);
									text-transform: uppercase;
									letter-spacing: 0.03em;
									margin-bottom: 9px;
								"
							>
								Single-vendor resilience
							</div>
							<div
								style="
									display: grid;
									grid-template-columns: repeat(2, 1fr);
									gap: 10px;
								"
							>
								<button
									v-for="entry in singleVendorPresets"
									:key="entry.key"
									@click="selectPreset(entry)"
									:disabled="!editable"
									:style="presetCardStyle(entry)"
								>
									<div style="font-size: 14px; font-weight: 600">
										{{ entry.label }}
									</div>
									<div
										style="
											font-size: 13px;
											color: var(--text-2);
											margin-top: 4px;
											line-height: 1.45;
										"
									>
										{{ entry.blurb }}
									</div>
								</button>
							</div>
						</div>
						<div v-if="crossVendorPresets.length">
							<div
								style="
									font-size: 13px;
									font-weight: 600;
									color: var(--text-2);
									text-transform: uppercase;
									letter-spacing: 0.03em;
									margin: 14px 0 9px;
								"
							>
								Cross-vendor strategies
							</div>
							<div
								style="
									display: grid;
									grid-template-columns: repeat(2, 1fr);
									gap: 10px;
								"
							>
								<button
									v-for="entry in crossVendorPresets"
									:key="entry.key"
									@click="selectPreset(entry)"
									:disabled="!editable"
									:style="presetCardStyle(entry)"
								>
									<div style="font-size: 14px; font-weight: 600">
										{{ entry.label }}
									</div>
									<div
										style="
											font-size: 13px;
											color: var(--text-2);
											margin-top: 4px;
											line-height: 1.45;
										"
									>
										{{ entry.blurb }}
									</div>
								</button>
							</div>
						</div>
					</div>
					<div
						v-if="selectedPreset && vendorsForPreset.length"
						style="
							margin-top: 12px;
							padding: 12px;
							background: var(--amber-bg);
							border: 1px solid var(--amber-bd);
							border-radius: 8px;
						"
					>
						<div
							style="
								font-size: 13px;
								color: var(--amber);
								font-weight: 600;
								margin-bottom: 8px;
							"
						>
							Provide API keys for this preset:
						</div>
						<div
							v-for="vendor in vendorsForPreset"
							:key="vendor"
							style="margin-bottom: 8px"
						>
							<label
								:style="{
									fontSize: '12px',
									color: 'var(--text-2)',
									display: 'block',
									marginBottom: '3px',
								}"
							>
								{{ providerLabel(vendor) }} API key<span
									v-if="missingVendors.includes(vendor)"
									style="color: var(--red)"
								>
									*</span
								>
							</label>
							<input
								:value="keysByVendor[vendor] || ''"
								@input="keysByVendor[vendor] = $event.target.value"
								type="password"
								:disabled="!editable"
								:placeholder="providerLabel(vendor) + ' API key'"
								style="
									width: 100%;
									padding: 9px 12px;
									font-size: 14px;
									border: 1px solid var(--border);
									border-radius: 6px;
									background: var(--surface);
									color: var(--text);
									font-family: inherit;
									box-sizing: border-box;
								"
							/>
						</div>
					</div>
				</div>

				<div class="jv-cfgpanel-acts">
					<button
						type="button"
						class="jv-btn jv-btn--sm jv-btn--ghost"
						@click="closePanel"
					>
						{{
							panel.source === "preset"
								? "Done"
								: panel.mode === "add"
								? "Cancel"
								: "Close"
						}}
					</button>
				</div>
			</div>
		</section>

		<!-- ================ QUICK / CUSTOM (shared rows) ================ -->
		<section v-if="singleMode" style="margin-bottom: 18px">
			<div
				v-if="!editorRows.length"
				style="font-size: 13px; color: var(--text-3); padding: 8px 0"
			>
				No models yet. Add one below.
			</div>

			<!-- Onboarding (singleMode) renders the connect content directly on the
           panel (preview .connect has no wrapper card); the Account editor keeps
           its bordered row cards. -->
			<div
				v-for="(m, i) in editorRows"
				:key="i"
				:style="
					singleMode
						? {}
						: {
								border: '1px solid var(--border)',
								borderRadius: '9px',
								padding: '10px',
								marginBottom: '8px',
								background: 'var(--surface-1)',
						  }
				"
			>
				<!-- Onboarding: two self-describing credential cards so the choice reads
             at a glance without extra copy. The compact toggle stays for the
             full (Account) editor's denser rows. -->
				<div v-if="singleMode" class="jv-ct">
					<div class="jv-ct-cards">
						<button
							v-for="opt in credTypes"
							:key="opt.value"
							type="button"
							class="jv-ct-card"
							:class="{ on: m.credentialType === opt.value }"
							@click="setCredType(m, opt.value)"
							:disabled="!editable"
							:aria-pressed="m.credentialType === opt.value"
						>
							<span class="jv-ct-ic">
								<svg
									v-if="opt.value === 'api_key'"
									viewBox="0 0 24 24"
									fill="none"
									stroke="currentColor"
									stroke-width="2"
									stroke-linecap="round"
									stroke-linejoin="round"
								>
									<path
										d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"
									/>
								</svg>
								<svg
									v-else
									viewBox="0 0 24 24"
									fill="none"
									stroke="currentColor"
									stroke-width="2"
									stroke-linecap="round"
									stroke-linejoin="round"
								>
									<path
										d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"
									/>
								</svg>
							</span>
							<span class="jv-ct-tx">
								<span class="jv-ct-t">{{ opt.label }}</span>
								<span class="jv-ct-d">{{ opt.desc }}</span>
							</span>
						</button>
					</div>
				</div>

				<!-- API-key credential. Onboarding (singleMode) lays the four fields out as
             a 2×2 grid so this view's height sits close to the subscription view -
             no jarring resize when toggling. The Account editor keeps the dense row. -->
				<div
					v-if="m.credentialType !== 'subscription'"
					:class="{ 'jv-single-body': singleMode }"
				>
					<div v-if="singleMode" class="jv-ak-grid">
						<JvCombo
							:model-value="m.provider"
							@update:model-value="(v) => onProviderChange(m, v)"
							:options="providerOptions"
							:editable="editable"
							placeholder="Provider"
						>
							<template #option="{ option }"
								><span style="display: inline-flex; align-items: center; gap: 8px"
									><ProviderLogo :provider="option.value" :size="16" />{{
										option.label
									}}</span
								></template
							>
							<template #selected="{ label, placeholder }"
								><span style="display: inline-flex; align-items: center; gap: 8px"
									><ProviderLogo v-if="label" :provider="label" :size="16" />{{
										label || placeholder
									}}</span
								></template
							>
						</JvCombo>
						<JvCombo
							:model-value="m.model"
							@update:model-value="
								(v) => {
									m.model = v;
								}
							"
							allow-custom
							:options="modelSuggestionsForProvider(m.provider)"
							:editable="editable"
							placeholder="Model ID (e.g. gpt-4o)"
						/>
						<input
							v-model="m.apiKey"
							:disabled="!editable"
							type="password"
							:placeholder="
								m.hasKey
									? 'key set, re-enter to change'
									: isLocalProviderRow(m)
									? 'Not required for local providers'
									: 'API key'
							"
						/>
						<input
							v-model="m.baseUrl"
							:disabled="!editable"
							placeholder="Base URL (OpenAI-compatible)"
						/>
					</div>
				</div>

				<!-- Chat-subscription credential. In the simplified (onboarding) editor
             the provider is enough: the Model ID field + rotation dropdown are
             hidden (model auto-defaults per provider), leaving just the provider
             picker + connect. The full account editor keeps all three. -->
				<div v-else :class="{ 'jv-single-body': singleMode }">
					<!-- Onboarding: just a Provider select. A chat subscription runs one
               fixed model per provider (auto-defaulted per onUpstreamChange /
               startConnect), so the model is not a user choice here - it is set
               behind the scenes and editable later in Settings → Account. -->
					<div v-if="singleMode" class="jv-pick">
						<div class="jv-fieldlab">Provider</div>
						<!-- Same-value guard: onUpstreamChange drops connected accounts (they
                 are provider-specific), so reselecting the CURRENT provider must
                 be a no-op rather than wiping a finished OAuth connect. -->
						<JvCombo
							:model-value="m.upstream"
							@update:model-value="
								(v) => {
									if (v === m.upstream) return;
									m.upstream = v;
									onUpstreamChange(m);
								}
							"
							:options="upstreamOpts"
							:editable="editable"
							placeholder="Provider"
						>
							<template #option="{ option }"
								><span style="display: inline-flex; align-items: center; gap: 8px"
									><ProviderLogo :upstream="option.value" :size="16" />{{
										option.label
									}}</span
								></template
							>
							<template #selected="{ label, placeholder }"
								><span style="display: inline-flex; align-items: center; gap: 8px"
									><ProviderLogo
										v-if="m.upstream"
										:upstream="m.upstream"
										:size="16"
									/>{{ label || placeholder }}</span
								></template
							>
						</JvCombo>
					</div>

					<!-- Connected accounts -->
					<div v-if="m.accounts && m.accounts.length" class="jv-pool-accts">
						<div class="jv-pool-lab">Connected accounts ({{ m.accounts.length }})</div>
						<div class="jv-pool-acctlist">
							<div
								v-for="(a, ai) in m.accounts"
								:key="a.account_ref || ai"
								class="jv-pool-acctchip"
							>
								<span class="jv-pool-avatar">{{
									(accountLabel(a) || "?").charAt(0).toUpperCase()
								}}</span>
								<span class="jv-pool-accttx">{{ accountLabel(a) }}</span>
								<span
									class="jv-pool-dot"
									:class="'jv-pool-dot--' + accountHealth(m).level"
									aria-hidden="true"
								></span>
								<span
									v-if="accountHealth(m).label"
									class="jv-pool-acct-health"
									:class="'jv-pool-acct-health--' + accountHealth(m).level"
									:title="accountHealth(m).title"
									>{{ accountHealth(m).label }}</span
								>
								<span class="jv-pool-acctacts">
									<button
										v-if="editable && !singleMode"
										class="jv-btn jv-btn--sm jv-btn--ghost"
										@click="startConnect(m, ai)"
										title="Re-authorize to mint fresh tokens"
									>
										Reconnect
									</button>
									<button
										v-if="editable"
										class="jv-btn jv-btn--sm jv-btn--ghost jv-pool-disc"
										@click="removeAccount(m, ai)"
									>
										Disconnect
									</button>
								</span>
							</div>
							<button
								v-if="editable && !singleMode && !(m._connect && m._connect.open)"
								@click="startConnect(m)"
								:disabled="
									m._connect && m._connect.loading && !m._connect.authorizeUrl
								"
								class="jv-pool-addrow"
							>
								+ Add account
							</button>
						</div>
					</div>
					<div
						v-else-if="!singleMode"
						style="font-size: 13px; color: var(--text-3); margin-bottom: 8px"
					>
						No accounts connected yet.
					</div>

					<!-- Onboarding: the two connect steps on a connected vertical spine
               (preview .csteps), always visible until an account is connected.
               Same handlers as the account editor's panel below: startConnect
               fetches the authorize URL (step 1's button turns into the real
               sign-in link), finishConnect submits the pasted callback URL. -->
					<template v-if="singleMode && !(m.accounts && m.accounts.length)">
						<div class="jv-cdivider"></div>
						<div class="jv-csteps">
							<!-- DEVICE-CODE (Kimi): show the code + verification link, poll for approval. -->
							<template v-if="m._connect && m._connect.deviceFlow">
								<div class="jv-cstep">
									<div class="jv-cnum">1</div>
									<div class="jv-cbody">
										<div class="jv-ctit">
											Sign in with {{ upstreamLabelOf(m.upstream) }}
										</div>
										<div class="jv-cdesc">
											Open the verification page, enter the code, and approve
											access. This panel updates automatically.
										</div>
										<div class="jv-crow" style="margin-top: 8px">
											<a
												v-if="m._connect.verificationUri"
												:href="m._connect.verificationUri"
												target="_blank"
												rel="noopener noreferrer"
												class="jv-cbtn jv-cbtn-primary"
												>Open verification page ↗</a
											>
										</div>
										<div
											v-if="m._connect.userCode"
											class="jv-cdesc"
											style="margin-top: 10px"
										>
											Code:
											<code
												style="
													font-size: 17px;
													letter-spacing: 2px;
													font-weight: 700;
												"
												>{{ m._connect.userCode }}</code
											>
										</div>
										<div
											v-if="m._connect.polling"
											class="jv-cdesc"
											style="margin-top: 8px"
										>
											Waiting for approval…
										</div>
										<div class="jv-cacts" style="margin-top: 10px">
											<button
												type="button"
												class="jv-cbtn jv-cbtn-ghost"
												@click="closeConnect(m)"
											>
												Cancel
											</button>
										</div>
									</div>
								</div>
							</template>
							<!-- PASTE-BACK (OpenAI/Google/xAI): open sign-in, paste the callback URL. -->
							<template v-else>
								<div class="jv-cstep">
									<div class="jv-cnum">1</div>
									<div class="jv-cbody">
										<div class="jv-chead">
											<div class="jv-ctit">
												Sign in with {{ upstreamLabelOf(m.upstream) }}
											</div>
											<div class="jv-crow">
												<a
													v-if="m._connect && m._connect.authorizeUrl"
													:href="m._connect.authorizeUrl"
													target="_blank"
													rel="noopener noreferrer"
													class="jv-cbtn jv-cbtn-primary"
													>Open sign-in ↗</a
												>
												<button
													v-else
													type="button"
													class="jv-cbtn jv-cbtn-primary"
													:disabled="
														!editable ||
														(m._connect && m._connect.loading)
													"
													@click="startConnect(m)"
												>
													{{
														m._connect && m._connect.loading
															? "Starting sign-in…"
															: "Open sign-in ↗"
													}}
												</button>
												<!-- Always offered, never gated behind "Open sign-in": signing in on
												     another device needs the URL WITHOUT opening a tab here. -->
												<button
													type="button"
													class="jv-cbtn jv-cbtn-ghost"
													:disabled="
														(m._connect && m._connect.loading) ||
														(!editable &&
															!(
																m._connect &&
																m._connect.authorizeUrl
															))
													"
													@click="copySigninLink(m)"
												>
													{{
														m._connect && m._connect.copied
															? "Copied ✓"
															: "Copy link"
													}}
												</button>
											</div>
										</div>
										<div class="jv-cdesc">
											Opens {{ upstreamLabelOf(m.upstream) }} in a new tab.
											Approve access, then come back here.
										</div>
									</div>
								</div>
								<div
									class="jv-cstep"
									:class="{
										'jv-pending': !(m._connect && m._connect.authorizeUrl),
									}"
								>
									<div class="jv-cnum">2</div>
									<div class="jv-cbody">
										<div class="jv-ctit">{{ pasteTitle(m.upstream) }}</div>
										<div class="jv-callout">
											<svg
												width="15"
												height="15"
												viewBox="0 0 24 24"
												fill="none"
												stroke="currentColor"
												stroke-width="1.9"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<circle cx="12" cy="12" r="9" />
												<path d="M12 8v5M12 16h.01" />
											</svg>
											<p v-if="isCodeOnlyPaste(m.upstream)">
												After you approve,
												{{ upstreamLabelOf(m.upstream) }} shows you an
												<b>authorization code</b>. Copy that code and paste
												it below.
											</p>
											<p v-else>
												After you approve, the browser shows a
												<b
													>&ldquo;This site can&rsquo;t be
													reached&rdquo;</b
												>
												page. That&rsquo;s expected: copy the
												<b>full URL from the address bar</b>
												(<kbd>⌘/Ctrl</kbd>+<kbd>L</kbd>, then
												<kbd>⌘/Ctrl</kbd>+<kbd>C</kbd>) and paste it below.
											</p>
										</div>
										<!-- Disabled until step 1 minted an authorize URL: a URL pasted
                       before sign-in has no nonce to pair with (finishConnect
                       would no-op), a silent dead-end. -->
										<input
											v-model="m._connect.pastedUrl"
											class="jv-paste"
											:disabled="
												!editable ||
												!(m._connect && m._connect.authorizeUrl)
											"
											:placeholder="
												pastePlaceholder(
													m.upstream,
													m._connect && m._connect.authorizeUrl
												)
											"
											@keydown.enter="finishConnect(m)"
										/>
										<div
											v-if="m._connect && m._connect.authorizeUrl"
											class="jv-cacts"
										>
											<button
												type="button"
												class="jv-cbtn jv-cbtn-ghost"
												@click="closeConnect(m)"
											>
												Cancel
											</button>
											<button
												type="button"
												class="jv-cbtn jv-cbtn-primary"
												:disabled="m._connect.loading"
												@click="finishConnect(m)"
											>
												{{
													m._connect.loading ? "Connecting…" : "Connect"
												}}
											</button>
										</div>
									</div>
								</div>
							</template>
						</div>
						<div v-if="m._connect && m._connect.error" class="jv-cn-err">
							{{ m._connect.error }}
						</div>
					</template>
				</div>
			</div>

			<button
				v-if="isMulti && editable"
				@click="addModel"
				class="jv-btn jv-btn--sm jv-btn--ghost"
			>
				+ Add model
			</button>
		</section>

		<!-- Save bar + sync status - hidden when a host renders its own footer. -->
		<div
			v-if="!footerless"
			class="jv-pool-savebar"
			style="
				display: flex;
				align-items: center;
				gap: 12px;
				flex-wrap: wrap;
				justify-content: flex-end;
			"
		>
			<span
				v-if="saveBlocked && missingVendors.length"
				style="font-size: 13px; color: var(--amber)"
			>
				Provide keys for: {{ missingVendors.map(providerLabel).join(", ") }}
			</span>
			<span v-else-if="dirty" style="font-size: 13px; color: var(--amber); font-weight: 600"
				>● Unsaved changes - Save configuration to apply</span
			>
			<span
				v-else-if="applyStatus.kind !== 'idle'"
				class="jv-pool-syncpill"
				:class="'jv-pool-syncpill--' + applyStatus.kind"
			>
				<span class="jv-pool-syncpill-ic" aria-hidden="true"></span>{{ applyStatus.text }}
			</span>
			<button
				v-if="editable"
				@click="save"
				:disabled="saving || saveBlocked"
				class="jv-btn jv-btn--primary"
			>
				{{ saving ? "Saving…" : "Save configuration" }}
			</button>
		</div>
	</div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from "vue";
import * as api from "@/api";
import {
	deriveMode,
	reorder,
	presetToModels,
	missingVendorKeys,
	validatePool,
	PROVIDER_LABELS,
	providerLabel,
	providerId,
	seedRowsFromConfig,
	defaultSubscriptionModel,
	subModelSuggestions,
	apiKeyModelHealth,
	subscriptionAccountHealth,
	dirtyAccountHealth,
	isCodeOnlyPaste,
	effectiveApiKey,
	LOCAL_PROVIDER_IDS,
} from "@/llm/pool";
import { errMessage as _err } from "@/lib/errors";
import { useConfirm } from "@/composables/useConfirm";
import JvCombo from "@/components/JvCombo.vue";
import DirectSubscriptionCard from "@/components/DirectSubscriptionCard.vue";
import ProviderLogo from "@/components/ProviderLogo.vue";
import { agentName } from "@/branding";

const { confirm } = useConfirm();

const props = defineProps({
	editable: { type: Boolean, default: true },
	// Which setup tabs to expose. Default = the full 3-mode editor (Account page).
	// Onboarding passes ["quick"] to offer a single direct model and hide the
	// proxy-pool Preset/Custom tabs + the Direct/Proxy badge - faster signup, no
	// failover/pooling decisions up front (users configure that later in Account).
	modes: { type: Array, default: () => ["quick", "preset", "custom"] },
	// Hide the built-in Save bar so a host (onboarding) can render its own footer
	// and trigger save() via a template ref (exposed below).
	footerless: { type: Boolean, default: false },
	// getDirectSubscriptionStatus() payload from the host (AiModelsPane), for a
	// tenant on the legacy flat-field DIRECT path (empty models[], creds live
	// outside this editor's config). null/absent = no direct subscription -
	// never passed by onboarding, which has nothing to probe yet. Only
	// is_direct_subscription synthesizes a row here; a merely-pooled single
	// subscription (is_single_subscription_pool) already renders as a normal
	// row via rows.value and needs no special-casing.
	directStatus: { type: Object, default: null },
});
const emit = defineEmits(["saved", "ready", "direct-changed"]);

// ---- state ---------------------------------------------------------------
const cfg = ref({ models: [], preset: "", routing_mode: "failover", proxy_active: false });
const catalog = ref([]);
// Admin-managed model catalog (jarvis.chat.api.get_model_catalog_ui): api-key
// suggestions, subscription suggestions, and per-provider defaults. Fetched on
// mount (see load()) independent of get_chat_ui_settings so this also works in
// the onboarding wizard, which never calls that. Falls back to the built-in
// literals below when the fetch fails or hasn't landed yet - never blank.
const modelCatalog = ref({
	api_key_models: {},
	subscription_models: {},
	default_models: {},
});
// Chat-subscription suggestion table derived from the fetched catalog (falls
// back to pool.js's built-in FALLBACK_SUB_MODELS via subModelSuggestions when
// empty/unfetched). Passed to every defaultSubscriptionModel(...) call site so
// an admin-changed subscription default is honoured everywhere, not just here.
const subscriptionSuggestions = computed(() =>
	subModelSuggestions(modelCatalog.value.subscription_models)
);
const rows = ref([]); // canonical camelCase rows (single source of truth)
const llmMode = ref("quick"); // "quick" | "preset" | "custom"
const selectedPreset = ref("");
const keysByVendor = ref({});
const err = ref("");
const saving = ref(false);
const sync = ref({
	last_sync_status: "",
	pending: false,
	subscription_status: "",
	warnings: [],
	model_statuses: [],
});
const savedSnapshot = ref("__init__"); // savable pool as of last load/save; drives the unsaved-changes notice
let pollTimer = null;

const ALL_MODE_TABS = [
	{ value: "quick", label: "Quick" },
	{ value: "preset", label: "Preset" },
	{ value: "custom", label: "Custom" },
];
// Only the tabs the host allows, in canonical order.
const modeTabs = computed(() => ALL_MODE_TABS.filter((t) => props.modes.includes(t.value)));
// With a single allowed mode the tab bar + Direct/Proxy badge are just noise -
// hide them and render that mode's body directly (onboarding's quick-only editor).
const singleMode = computed(() => modeTabs.value.length <= 1);
// Whether the single-mode (onboarding) row is savable - an account is connected,
// or an API key + provider/model are filled. Emitted so the host footer can
// invite the final "Onboard Jarvis" click once the user is ready.
const ready = computed(() => {
	if (!singleMode.value) return false;
	const r = rows.value[0];
	if (!r) return false;
	if (r.credentialType === "subscription")
		return (r.accounts || []).some((a) => a && (a.oauth_blob || a.account_ref));
	return !!(
		(r.provider || "").trim() &&
		(r.model || "").trim() &&
		((r.apiKey || "").trim() || r.hasKey || isLocalProviderRow(r))
	);
});
const credTypes = [
	{
		value: "subscription",
		label: "Chat subscription",
		desc: "Sign in with your ChatGPT or Gemini plan",
	},
	{
		value: "api_key",
		label: "API key",
		desc: "Bring your own key from OpenAI, Anthropic and more",
	},
];
// (rotationOpts removed with the Account-rotation select. The VALUE still ships:
// newRow()/setCredType seed rotation:"sticky", matching the schema default. Restore
// this list if the control ever comes back.)
const upstreamOpts = [
	{ value: "openai", label: "OpenAI" },
	{ value: "google", label: "Google Gemini" },
	{ value: "xai", label: "xAI Grok" },
	{ value: "kimi", label: "Kimi (Moonshot)" },
];
// upstream value -> the OAuth provider label the backend _PROVIDER_OAUTH_MAP is
// keyed by (begin_pool_account_signin needs the label, not the upstream value).
// MUST match jarvis/oauth/providers.py _PROVIDER_OAUTH_MAP keys.
const UPSTREAM_OAUTH_PROVIDER = {
	openai: "OpenAI",
	google: "Google Gemini",
	xai: "xAI Grok",
	kimi: "Kimi (Moonshot)",
};
// JvCombo speaks display LABELS; a row stores `upstream` as the VALUE the pool spec
// requires ("openai" / "google"). Bridge the two rather than letting "OpenAI" reach
// the spec (the fleet validates upstream against openai|anthropic|google and 422s).
const upstreamLabels = upstreamOpts.map((o) => o.label);
// Fall back to the raw value (never blank) so an unrecognized/legacy upstream
// renders "Sign in with <value>" rather than "Sign in with " (review finding).
const upstreamLabelOf = (v) =>
	(upstreamOpts.find((o) => o.value === v) || {}).label || v || "your provider";
const upstreamValueOf = (l) => (upstreamOpts.find((o) => o.label === l) || {}).value || l;
// Upstreams whose approval screen hands back a BARE authorization code instead
// of redirecting to a callback URL the customer can copy from the address bar.
// isCodeOnlyPaste (xAI) is imported from @/llm/pool so the pool editor and the
// direct subscription card share one answer rather than each keeping a copy.
const pasteTitle = (u) => (isCodeOnlyPaste(u) ? "Paste the code" : "Paste the callback URL");
const pastePlaceholder = (u, ready) => {
	if (!ready)
		return isCodeOnlyPaste(u)
			? "Complete step 1 first, then paste the code here"
			: "Complete step 1 first, then paste the URL here";
	return isCodeOnlyPaste(u)
		? "Paste the code shown after you approve"
		: "http://localhost:1455/auth/callback?code=…";
};
// Provider dropdown fed by the shared PROVIDER_LABELS (id⇄label). Rows store the
// display LABEL as `provider` (matches seedRowsFromConfig + the desk page).
const providerOptions = PROVIDER_LABELS.map((p) => p.label);

// ---- model-id suggestions --------------------------------------------------
// STATIC_MODEL_SUGGESTIONS (the hardcoded per-provider datalist) is gone: the
// admin-managed catalog (modelCatalog.value.api_key_models, fetched in load())
// is now the source, so a model added in the admin desk shows up here with no
// deploy. See modelSuggestionsForProvider below.
const PROVIDER_DEFAULTS = {
	Anthropic: { model: "claude-sonnet-4-6", baseUrl: "https://api.anthropic.com" },
	// "gpt-5.5" is this literal's FALLBACK value only, used before the catalog
	// fetch lands or if it fails - providerDefaultModel() below prefers the
	// catalog's is_default flag. Previously a stale "gpt-4o" here (fixed
	// alongside the catalog wiring: PROVIDER_DEFAULTS.OpenAI predates the
	// gpt-5.x rollout and was never updated).
	OpenAI: { model: "gpt-5.5", baseUrl: "https://api.openai.com/v1" },
	"Google Gemini": {
		model: "gemini-2.5-pro",
		baseUrl: "https://generativelanguage.googleapis.com",
	},
	Mistral: { model: "mistral-large-latest", baseUrl: "https://api.mistral.ai/v1" },
	Groq: { model: "llama-3.3-70b-versatile", baseUrl: "https://api.groq.com/openai/v1" },
	"Together AI": {
		model: "meta-llama/Llama-3.3-70B-Instruct-Turbo",
		baseUrl: "https://api.together.xyz/v1",
	},
	DeepSeek: { model: "deepseek-chat", baseUrl: "https://api.deepseek.com" },
	"Moonshot (Kimi)": { model: "kimi-k2.6", baseUrl: "https://api.moonshot.ai/v1" },
	"xAI Grok": { model: "grok-4.5", baseUrl: "https://api.x.ai/v1" },
	"GLM / Z.ai": { model: "glm-4.6", baseUrl: "https://api.z.ai/api/paas/v4" },
	// GLM Coding Plan is a separate z.ai subscription from pay-as-you-go "GLM / Z.ai"
	// above - a coding-plan key 402s with "insufficient balance" on the pay-as-you-go
	// base URL even though it's perfectly valid on this one (see apiKeyModelHealth's
	// targeted hint in pool.js for the exact trap this option exists to avoid).
	"GLM / Z.ai (Coding Plan)": {
		model: "glm-4.6",
		baseUrl: "https://api.z.ai/api/coding/paas/v4",
	},
	OpenRouter: { model: "anthropic/claude-sonnet-4-6", baseUrl: "https://openrouter.ai/api/v1" },
	"Ollama (local)": { model: "llama3", baseUrl: "http://host.docker.internal:11434/v1" },
	"vLLM (local)": { model: "", baseUrl: "" },
	"OpenAI-Compatible": { model: "", baseUrl: "" },
};
function catalogVendorLabel(vid) {
	return vid === "gemini" ? "Google Gemini" : providerLabel(vid);
}
// The api-key-tier model preselected for a provider. Prefers the admin-managed
// catalog's is_default flag; falls back to the PROVIDER_DEFAULTS literal when
// the catalog has no default for this label (fetch still pending, failed, or
// the label has no api-key rows at all).
function providerDefaultModel(label) {
	const rows = (modelCatalog.value.api_key_models || {})[label] || [];
	const flagged = rows.find((m) => m.is_default);
	return (flagged && flagged.model_id) || (PROVIDER_DEFAULTS[label] || {}).model || "";
}
function modelSuggestionsForProvider(provider) {
	const label = providerLabel(provider || "");
	const out = [];
	const push = (id) => {
		if (id && out.indexOf(id) === -1) out.push(id);
	};
	(catalog.value || []).forEach((e) =>
		(e.models || []).forEach((m) => {
			if (catalogVendorLabel(m.provider) === label) push(m.model);
		})
	);
	((modelCatalog.value.api_key_models || {})[label] || []).forEach((m) => push(m.model_id));
	push(providerDefaultModel(label));
	return out;
}

// ---- derived -------------------------------------------------------------
const isMulti = computed(() => llmMode.value === "custom");
const editorRows = computed(() => (isMulti.value ? rows.value : rows.value.slice(0, 1)));
const singleVendorPresets = computed(() =>
	catalog.value.filter((c) => c.kind === "single_vendor")
);
const crossVendorPresets = computed(() => catalog.value.filter((c) => c.kind === "cross_vendor"));
const selectedEntry = computed(
	() => catalog.value.find((c) => c.key === selectedPreset.value) || null
);
const vendorsForPreset = computed(() => {
	const e = selectedEntry.value;
	if (!e) return [];
	if (e.vendors && e.vendors.length) return e.vendors;
	const seen = new Set(),
		out = [];
	for (const m of e.models || [])
		if (!seen.has(m.provider)) {
			seen.add(m.provider);
			out.push(m.provider);
		}
	return out;
});
const missingVendors = computed(() => {
	const e = selectedEntry.value;
	return e ? missingVendorKeys(e, keysByVendor.value) : [];
});
// Not llmMode-gated: llmMode is always "custom" for the settings (!singleMode)
// editor now (Quick/Preset tabs are gone), so this only needs to ask "is a
// preset currently selected (via selectPreset, reused verbatim by the
// config-section's 'From a preset' source) with vendor keys still missing?"
const saveBlocked = computed(() => !!selectedPreset.value && missingVendors.value.length > 0);

// Direct/Proxy badge - mirrors jarvis_account.js renderModeBadge().
// Quick is always Direct (single model); Preset is Proxy once chosen; Custom
// derives from the count of valid rows via the shared deriveMode helper.
// Valid (fillable) rows - shared by the badge mode + label. A subscription row
// needs a model id; an api_key row needs provider + model.
const validModels = computed(() =>
	rows.value.filter(
		(r) =>
			r &&
			(r.credentialType === "subscription"
				? (r.model || "").trim()
				: (r.provider || "").trim() && (r.model || "").trim())
	)
);
const badgeMode = computed(() => {
	// Quick is a single model: DIRECT for api_key, but a chat-subscription row
	// forces the cliproxy/proxy path (compute_proxy_active), so reflect that.
	if (llmMode.value === "quick") {
		const r0 = rows.value[0];
		return r0 && r0.credentialType === "subscription" ? "proxy" : "direct";
	}
	if (llmMode.value === "preset") return selectedPreset.value ? "proxy" : "direct";
	return deriveMode(validModels.value, null);
});
// Human label. "failover" only makes sense with ≥2 models (a preset ladder or a
// multi-row custom pool). A lone chat subscription is still proxied (it needs
// the cliproxy sidecar) but has nothing to fail over to, so it reads plain
// "Proxy" rather than the misleading "Proxy (failover)".
const badgeLabel = computed(() => {
	// Only badge a real multi-model FAILOVER pool. A single model - a direct
	// api-key OR a lone chat subscription - shows NO badge: it was just noise, and
	// "Proxy" on a single subscription read as confusing/broken.
	if (llmMode.value === "preset") return selectedPreset.value ? "Proxy (failover)" : "";
	if (llmMode.value === "custom") return validModels.value.length >= 2 ? "Proxy (failover)" : "";
	return ""; // quick = single model
});
// Save-bar status pill (Option A - "honest model health"). Reflects the outcome
// of the most recent apply, including any per-account subscription warnings the
// backend surfaced (e.g. a chat subscription that rejected a test request).
const applyStatus = computed(() => {
	if (sync.value.pending) return { kind: "pending", text: "Applying to your agent…" };
	const s = sync.value.last_sync_status || "";
	if (s.startsWith("failed")) return { kind: "failed", text: "Sync failed — try again" };
	if (s.startsWith("ok")) {
		let n = Array.isArray(sync.value.warnings) ? sync.value.warnings.length : 0;
		if (n === 0 && sync.value.subscription_status === "unverified") n = 1;
		if (n > 0)
			return {
				kind: "warn",
				text: `Applied · ${n} model${n > 1 ? "s" : ""} need${n > 1 ? "" : "s"} attention`,
			};
		return { kind: "ok", text: "Applied" };
	}
	return { kind: "idle", text: "" };
});
// Unsaved-changes detector: current savable pool vs the last saved snapshot.
// Connecting an account mutates rows in memory (the fresh OAuth blob lives only
// here until saved), so this lights up the "Unsaved changes" notice.
const dirty = computed(
	() =>
		savedSnapshot.value !== "__init__" &&
		!saving.value &&
		poolSnapshot() !== savedSnapshot.value
);

// ---- helpers -------------------------------------------------------------
function blankConnect() {
	return {
		open: false,
		loading: false,
		error: "",
		copied: false,
		nonce: "",
		authorizeUrl: "",
		pastedUrl: "",
		reconnectIdx: null,
		deviceFlow: false,
		userCode: "",
		verificationUri: "",
		polling: false,
	};
}
function presetCardStyle(entry) {
	const on = selectedPreset.value === entry.key;
	return {
		padding: "14px 16px",
		fontSize: "14px",
		cursor: props.editable ? "pointer" : "default",
		borderRadius: "10px",
		textAlign: "left",
		border: on ? "2px solid var(--cta)" : "1px solid var(--border)",
		background: on ? "var(--cta-bg)" : "var(--surface)",
		color: on ? "var(--cta)" : "var(--text)",
		opacity: entry.enabled === false ? "0.45" : "1",
		fontWeight: on ? "600" : "400",
	};
}

// Copy text to clipboard, with a graceful fallback for insecure (LAN HTTP)
// contexts where navigator.clipboard is undefined (ported from the desk page).
function copyTextWithFallback(text) {
	if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
	return new Promise((resolve, reject) => {
		const ta = document.createElement("textarea");
		ta.value = text;
		ta.style.position = "fixed";
		ta.style.left = "-9999px";
		ta.style.top = "0";
		document.body.appendChild(ta);
		ta.focus();
		ta.select();
		try {
			const ok = document.execCommand("copy");
			document.body.removeChild(ta);
			ok ? resolve() : reject(new Error("copy failed"));
		} catch (e) {
			document.body.removeChild(ta);
			reject(e);
		}
	});
}

// Compact "source" label for a list row (unified failover list, !singleMode
// only) - e.g. "Subscription · OpenAI" / "API key · Anthropic".
function sourceChip(row) {
	if (!row) return "";
	if (row.credentialType === "subscription")
		return "Subscription · " + (row.upstream === "google" ? "Google" : "OpenAI");
	return "API key · " + (row.provider || "—");
}

// ---- master-detail config section (!singleMode only) --------------------
// panel: which row is being added/edited, and which source tab is active.
// "preset" only applies in add-mode - picking a card replaces the whole pool
// (selectPreset, reused verbatim) rather than editing panelRow.
// The panel targets its row by IDENTITY (uid), never by array index: reorder and
// remove mutate `rows` while the panel is open, and an index would silently repoint
// it at a neighbour mid-OAuth. _uid is a client-only handle -- buildSaveModels maps
// explicit fields, so it never reaches the payload.
// testing/testResult drive the API-key "Test" button (below): per-PANEL, not
// per-row, since only one panel is ever open at a time - testResult is cleared
// whenever the panel's row identity changes OR its own provider/model/apiKey/
// baseUrl fields are edited (a stale green check must not survive an edit).
// testGen additionally guards testApiKeyRow's in-flight request against a stale
// response landing after an edit (see that function's doc comment).
const panel = ref({
	open: false,
	mode: "add",
	uid: null,
	source: "subscription",
	addBackups: true,
	testing: false,
	testResult: null,
	testGen: 0,
});
const panelRow = computed(() => rows.value.find((r) => r._uid === panel.value.uid) || null);
// A removed panel row leaves panelRow null; close rather than render a headless panel.
watch(panelRow, (r) => {
	if (panel.value.open && !r) panel.value = closedPanel();
});
// Invalidate a stale Test verdict the instant any field it depends on changes -
// otherwise editing the key after a failed test would leave the old red result
// on screen, implying it still applies to what's now typed in. Array-of-getters
// form (not a joined string key: two different field combos could join to the
// same string, e.g. provider="a",model="b c" vs provider="a b",model="c") -
// same idiom AgentsList.vue already uses for a multi-source watch. Bumping
// testGen here (not just nulling testResult) also ABANDONS an in-flight Test:
// its response, when it lands, will see the generation mismatch and skip
// writing testResult/testing (testApiKeyRow's `stale()` guard) - so `testing`
// must be reset to false HERE too, or the button would stay stuck on
// "Testing…" until that now-irrelevant response arrives (if it ever does).
watch(
	[
		() => panelRow.value?.provider,
		() => panelRow.value?.model,
		() => panelRow.value?.apiKey,
		() => panelRow.value?.baseUrl,
	],
	() => {
		panel.value.testResult = null;
		panel.value.testGen++;
		panel.value.testing = false;
	}
);
function closedPanel() {
	return {
		open: false,
		mode: "add",
		uid: null,
		source: "subscription",
		addBackups: true,
		testing: false,
		testResult: null,
		testGen: 0,
	};
}
function isRowEmpty(r) {
	if (!r) return true;
	if (r.credentialType === "subscription") return !(r.accounts || []).length;
	return !(r.model || "").trim() && !(r.apiKey || "").trim() && !r.hasKey;
}
// Append a blank row up-front (not on a later "commit") so finishConnect's
// !footerless auto-save - which can fire while this panel is still open -
// already includes it instead of silently dropping an in-progress connect.
function openAdd() {
	const r = { ...newRow(), order: rows.value.length };
	// Open a NEW row on Chat subscription. newRow() seeds credentialType "api_key"
	// (it is the shape the row object defaults to), which meant "+ Add a model"
	// always landed on the API-key tab regardless of what the customer already had.
	// Subscription is the path most customers take -- sign in with a plan they own,
	// no key to paste -- so it is the better first stop. (It has never followed the
	// last row's type; that would be unpredictable.)
	setCredType(r, "subscription");
	rows.value = [...rows.value, r];
	panel.value = {
		open: true,
		mode: "add",
		uid: r._uid,
		source: "subscription",
		addBackups: true,
		testing: false,
		testResult: null,
		testGen: 0,
	};
}
function openEdit(i) {
	const r = rows.value[i];
	if (!r) return;
	panel.value = {
		open: true,
		mode: "edit",
		uid: r._uid,
		source: r.credentialType === "subscription" ? "subscription" : "api_key",
		addBackups: true,
		testing: false,
		testResult: null,
		testGen: 0,
	};
}

// ---- pre-save "Test" (API-key rows only) ---------------------------------
// Provider ids whose usual endpoint only makes sense reached from INSIDE the
// tenant's bifrost container (localhost / a customer LAN), never from this
// browser's bench - LOCAL_PROVIDER_IDS is shared with pool.js's own key-
// optionality check (both MUST match jarvis.llm_key_probe.LOCAL_PROVIDER_IDS).
// The Test button still runs (a customer CAN point "vllm"/"ollama" at a real
// public URL), this only softens the promise with an upfront disclaimer
// instead of silently implying a guarantee the bench can't make.
function isLocalProviderRow(row) {
	return !!(row && LOCAL_PROVIDER_IDS.has(providerId(row.provider)));
}
// Why the Test button is disabled right now, or "" when it's enabled. hasKey +
// a blank apiKey means an already-saved key that hasn't been re-typed - the
// probe only ever sees what's in the panel (it never reads the stored,
// encrypted secret), so it has nothing to send yet.
function testBlockedReason(row) {
	if (!row) return "Nothing to test";
	if (!(row.provider || "").trim()) return "Choose a provider to test";
	if (!(row.model || "").trim()) return "Enter a model id to test";
	// Local providers (Ollama, vLLM) take no key - nothing blocks the probe.
	if (!(row.apiKey || "").trim() && !isLocalProviderRow(row))
		return row.hasKey ? "Re-enter the key to test it" : "Enter an API key to test";
	return "";
}
function testButtonHint(row) {
	if (isLocalProviderRow(row)) {
		return (
			"Sends a minimal live request from the bench. Local/private endpoints (ollama, " +
			"vllm) can only be fully verified from inside your Jarvis container - a pass here " +
			"doesn't guarantee the container can reach it too."
		);
	}
	return "Sends a minimal live request to this provider using what's typed above. Nothing is saved.";
}
// Effective base_url to send: the row's own value, falling back to the provider's
// known default. A row a customer saved on a STANDARD provider (OpenAI/Anthropic/...)
// legitimately stores no base_url at all (build_pool_payload only emits one when
// present; validatePool doesn't require one outside NEEDS_BASE_URL) - onProviderChange
// only fills it in when the provider is freshly PICKED, not when Edit re-opens an
// already-saved row. Without this fallback, Test on any such existing row always
// failed with "Enter a base URL before testing." even though Save would succeed.
function effectiveTestBaseUrl(row) {
	const own = ((row && row.baseUrl) || "").trim();
	if (own) return own;
	return (PROVIDER_DEFAULTS[row && row.provider] || {}).baseUrl || "";
}
// Live, side-effect-free probe (jarvis.llm_key_probe.test_llm_api_key) of whatever is
// currently typed into the panel - never persists, never touches the fleet/container, and
// is NOT a substitute for (must never call) the mutating /llm-pool apply. Motivated by a
// real GLM/Z.ai case: a valid key on a zero-balance account saved cleanly and only failed
// AFTER save with a bare "Not working" chip - this surfaces the provider's OWN error
// (e.g. "Insufficient balance or no resource package. Please recharge.") before Save.
//
// Race guard: `panel` is a ref whose `.value` is WHOLESALE REPLACED (not mutated) by
// openAdd/openEdit/closePanel, and this function awaits a network round-trip in between
// reading and writing it - so a slow response for row A landing after the customer closed
// A's panel and opened B's must never overwrite B's testing/testResult (the same class of
// bug the OAuth-connect flow elsewhere in this file guards against with its nonce check).
// `myPanel` pins the EXACT panel object this call started on (object identity, not just a
// uid - a closed-then-reopened panel on the same row is a different object); `testGen` is
// bumped both here and by the field-edit watch() below, so an in-flight response is also
// discarded if the customer edits the row while waiting (otherwise the watch's clear could
// be immediately undone by a stale response landing after it).
async function testApiKeyRow(row) {
	if (!row || panel.value.testing || testBlockedReason(row)) return;
	const myPanel = panel.value;
	const myGen = ++myPanel.testGen;
	const stale = () => panel.value !== myPanel || myPanel.testGen !== myGen;
	myPanel.testing = true;
	myPanel.testResult = null;
	try {
		const res = await api.testLlmApiKey({
			provider: row.provider || "",
			model: row.model || "",
			api_key: row.apiKey || "",
			base_url: effectiveTestBaseUrl(row),
		});
		if (stale()) return;
		const checks = Array.isArray(res && res.checks) ? res.checks : [];
		const last = checks[checks.length - 1];
		myPanel.testResult = {
			ok: !!(res && res.ok),
			message:
				(last && last.detail) ||
				(res && res.ok ? "The provider accepted the request." : "The test failed."),
			caveat: (res && res.caveat) || "",
		};
	} catch (e) {
		if (stale()) return;
		myPanel.testResult = { ok: false, message: _err(e), caveat: "" };
	} finally {
		if (!stale()) myPanel.testing = false;
	}
}

// Resilient-by-default (API KEYS ONLY - no subscription presets exist and
// multi-model-per-account is unconfirmed for cliproxy, so subscriptions never
// auto-add backups). Finds the catalog's single-vendor preset for this
// provider, if any.
function vendorSinglePreset(provider) {
	const pid = providerId(provider);
	return catalog.value.find(
		(c) =>
			c.kind === "single_vendor" &&
			(c.models || []).length > 0 &&
			(c.models || []).every((m) => m.provider === pid)
	);
}
// Expand a freshly-added api_key row into its provider's full single-vendor
// failover chain, sharing the same key - additive only (never touches other
// rows), and only for models not already present for this provider.
function expandApiKeyBackups(r) {
	const preset = vendorSinglePreset(r.provider);
	if (!preset) return;
	const models = presetToModels(preset, {});
	const existing = new Set(
		rows.value
			.filter((x) => x.credentialType === "api_key" && x.provider === r.provider)
			.map((x) => x.model)
	);
	const toAdd = models.filter((m) => m.model !== r.model && !existing.has(m.model));
	if (!toAdd.length) return;
	const base = rows.value.length;
	const extra = toAdd.map((m, i) => ({
		_uid: nextUid(),
		provider: r.provider,
		model: m.model,
		apiKey: r.apiKey,
		baseUrl: r.baseUrl,
		hasKey: false,
		credentialType: "api_key",
		rotation: "sticky",
		upstream: "openai",
		accounts: [],
		_connect: blankConnect(),
		order: base + i,
	}));
	rows.value = [...rows.value, ...extra];
}
// List row's "Reconnect" shortcut: open the panel with the sign-in steps ready
// (re-using the first account's slot if one exists) instead of making the user
// find "+ Add account" inside the panel themselves.
//
// It used to call startConnect here, which fires OAuth immediately -- so Reconnect
// hurled you at the provider's login before you saw a single instruction. Same bug
// as "+ Connect account" had. It now opens the panel; step 1's "Open sign-in" starts
// OAuth, inside that click (which is what keeps the popup-blocker fix working).
function quickReconnect(i) {
	const r = rows.value[i];
	if (!r) return;
	openEdit(i);
	openConnectPanel(r, r.accounts && r.accounts.length ? 0 : null);
}
function setPanelSource(src) {
	panel.value.source = src;
	if (src === "preset") return;
	const r = panelRow.value;
	if (r) setCredType(r, src);
}
// Closing the panel (Cancel/Done/Close) - an add-row that was opened but
// never filled in (no preset picked) is dropped so an abandoned "+ Add
// model" doesn't leave a dead row in the pool.
function closePanel() {
	const r = panelRow.value;
	// Add-mode api_key row, checkbox on, filled in: expand into the vendor's
	// resilience chain before the empty-row cleanup below (a freshly-expanded
	// row is never "empty").
	if (
		panel.value.mode === "add" &&
		panel.value.source === "api_key" &&
		panel.value.addBackups &&
		r &&
		(r.provider || "").trim() &&
		((r.apiKey || "").trim() || r.hasKey)
	) {
		expandApiKeyBackups(r);
	}
	if (panel.value.mode === "add" && panel.value.source !== "preset" && r && isRowEmpty(r)) {
		rows.value = rows.value.filter((x) => x._uid !== r._uid);
	}
	panel.value = closedPanel();
}

// ---- direct subscription (legacy flat-field path) as a list row ---------
// !singleMode only - onboarding never passes directStatus. Rendered OUTSIDE
// rows.value/save() entirely (verdict §3: never round-trip a direct row
// through save_llm_pool, which would migrate direct -> proxy); DirectSubscriptionCard
// keeps owning the actual reauthorize/disconnect flow, unchanged.
const showDirectRow = computed(
	() => !singleMode.value && !!(props.directStatus && props.directStatus.is_direct_subscription)
);
const directPanelOpen = ref(false);
watch(
	() => props.directStatus,
	(v) => {
		if (!v || !v.is_direct_subscription) directPanelOpen.value = false;
	}
);
function onDirectCardChanged() {
	directPanelOpen.value = false;
	emit("direct-changed");
}
async function removeDirect() {
	if (
		!(await confirm({
			title: "Disconnect chat subscription?",
			message: `${agentName} chat will stop working until you reconnect.`,
			confirmLabel: "Disconnect",
			danger: true,
		}))
	)
		return;
	try {
		const res = await api.disconnectSubscription();
		if (!res || res.ok === false) {
			err.value = (res && res.error && res.error.message) || "Disconnect failed.";
			return;
		}
		directPanelOpen.value = false;
		emit("direct-changed");
	} catch (e) {
		err.value = _err(e);
	}
}

// What the list row shows in the model cell.
// This used to be `row.model || row.provider || '—'`, which was wrong for a
// SUBSCRIPTION row: `provider` belongs to the api-key shape and is never cleared
// when the row is switched to a subscription, so whatever api-key provider was
// last picked (or none - newRow() now seeds `provider: ""`, not a default like
// "Anthropic") leaks through. A row whose chip correctly read "Subscription ·
// OpenAI" could display a stray provider name in the model column instead of
// its own model. Never fall back to `provider` here.
function rowModelLabel(row) {
	if (row.model) return row.model;
	if (row.credentialType === "subscription") return "Model not set";
	return row.provider || "—";
}

// Monotonic client-only row handle. Every row that can reach the failover list gets
// one so the config panel can hold a stable reference across reorder/remove.
let _uidSeq = 0;
function nextUid() {
	return ++_uidSeq;
}

function newRow() {
	return {
		_uid: nextUid(),
		provider: "",
		model: "",
		apiKey: "",
		baseUrl: "",
		hasKey: false,
		credentialType: "api_key",
		rotation: "sticky",
		upstream: "openai",
		accounts: [],
		_connect: blankConnect(),
		order: 0,
	};
}

function setCredType(m, type) {
	m.credentialType = type;
	if (type === "subscription") {
		if (!m.rotation) m.rotation = "sticky";
		if (!m.upstream) m.upstream = "openai";
		if (!Array.isArray(m.accounts)) m.accounts = [];
		if (!m._connect) m._connect = blankConnect();
		// The model field is hidden for chat subscriptions in BOTH editors now (a plan
		// grants its model; typing a model id was busywork and an easy way to enter an
		// invalid one). validatePool + save still REQUIRE a model id, so derive it from
		// the chosen provider. Dropping this would make every subscription save fail
		// validation with "model is required".
		m.model = defaultSubscriptionModel(m.upstream, subscriptionSuggestions.value);
	} else {
		// Toggling back to API key: drop the subscription's model id so it doesn't
		// linger under an API-key provider it does not belong to (a "gpt-5.5" left on
		// an Anthropic api-key row saves a provider/model mismatch that only fails at
		// the upstream). This used to be gated on singleMode -- but the SETTINGS editor
		// hides the subscription model field too, so it needs the same reset; without it
		// the stale id is invisible AND unsavable-by-hand.
		m.model = providerDefaultModel(m.provider);
	}
}
function onProviderChange(m, newProvider) {
	// Only act on an ACTUAL provider switch (re-selecting the same one is a no-op).
	const changed = newProvider !== m.provider;
	m.provider = newProvider;
	if (!changed) return;
	// Snap the model + base URL to the NEW provider's defaults, replacing any
	// leftover from the previous provider — so picking "GLM / Z.ai" gives glm-4.6,
	// not whatever model was there before. Providers with no default model
	// (OpenAI-Compatible / vLLM) clear the field so the user types their own.
	const d = PROVIDER_DEFAULTS[m.provider] || {};
	m.model = providerDefaultModel(m.provider);
	m.baseUrl = d.baseUrl || "";
	// A stored key (hasKey) belongs to the OLD provider's key_ref, not this one -
	// carrying it forward would either merge the wrong provider's secret on save
	// (onboarding.py's merge-by-provider fallback keys on the NEW provider, so it
	// actually finds nothing) or, for a fresh switch to Ollama/vLLM, leave the row
	// looking "has a key" while save sends a blank api_key - reproducing the exact
	// "api_key is blank on an enabled model" rejection this switch already causes
	// between any two providers. Same reasoning as onUpstreamChange dropping
	// connected accounts on a subscription upstream switch, just below.
	m.hasKey = false;
	m.apiKey = "";
}
// Provider switch on a subscription row in the simplified onboarding editor:
// re-default the (hidden) model AND drop any already-connected account, which is
// provider-specific - otherwise we'd save a model bound to the wrong provider's
// OAuth credential. A no-op elsewhere (full editor manages model/accounts itself).
function onUpstreamChange(m) {
	if (m.credentialType !== "subscription") return;
	// Was gated on singleMode; the settings editor now hides the model field too, so
	// it needs the same derivation. Changing provider must also clear the accounts:
	// an OAuth account is authorized against ONE provider, so keeping OpenAI accounts
	// on a row switched to Anthropic would ship a pool whose credentials can't serve it.
	m.model = defaultSubscriptionModel(m.upstream, subscriptionSuggestions.value);
	m.accounts = [];
	m._connect = blankConnect();
}
function move(i, d) {
	rows.value = reorder(rows.value, i, i + d);
}
async function remove(i) {
	const r = rows.value[i];
	if (!r) return;
	const label = rowModelLabel(r);
	if (
		!(await confirm({
			title: "Remove this model?",
			message: label
				? `"${label}" will be removed from the failover list. Save configuration to apply.`
				: "This model will be removed from the failover list. Save configuration to apply.",
			confirmLabel: "Remove",
			danger: true,
		}))
	)
		return;
	// Filter by the row's stable handle, not the captured index: confirm() awaits, so
	// an index could go stale if rows.value is re-seeded meanwhile.
	rows.value = rows.value.filter((x) => x._uid !== r._uid);
}
function removeAccount(m, idx) {
	m.accounts = (m.accounts || []).filter((_, j) => j !== idx);
}
function addModel() {
	rows.value = [...rows.value, { ...newRow(), order: rows.value.length }];
}

function selectPreset(entry) {
	selectedPreset.value = entry.key;
	rows.value = seedFromPreset(entry);
}
function seedFromPreset(entry) {
	// Every row needs a unique _uid: remove() deletes by _uid, so preset rows that
	// shared an undefined _uid would all vanish on removing any one of them.
	return presetToModels(entry, keysByVendor.value).map((m) => ({
		_uid: nextUid(),
		provider: providerLabel(m.provider),
		model: m.model,
		apiKey: m.api_key || "",
		baseUrl: "",
		hasKey: false,
		credentialType: "api_key",
		rotation: "sticky",
		upstream: "openai",
		accounts: [],
		_connect: blankConnect(),
		order: m.order,
	}));
}

// ---- connect flow (paste-back OAuth) -------------------------------------
function accountLabel(a) {
	// Show a real label / email; never surface the internal SUB_<hex> account ref.
	const l = (a && a.label) || "";
	if (l && !/^SUB_/i.test(l)) return l;
	return (a && a.account_email) || "Account connected";
}
function firstWarningMessage() {
	return (sync.value.warnings && sync.value.warnings[0] && sync.value.warnings[0].message) || "";
}
// Honest model health: the connected-account dot + label for a model row.
// Subscriptions reflect the fleet's last (pool-wide) subscription-probe result via
// subscriptionAccountHealth (@/llm/pool.js, shared with onboarding below); api-key
// rows reflect their own per-model verdict from the last apply (contract 1.11
// model_statuses).
//
// Onboarding (singleMode) USED to hardcode {level:"neutral"} unconditionally here,
// before looking at any real signal - and the CSS painted "neutral" the exact same
// green as a positively-verified "ok", so an unverified, out-of-quota account
// rendered identically to a healthy one (2026-07-23 trace: the customer saw a green
// dot + "Account connected" for a ChatGPT account that had no quota left). Fixed by
// actually reading sync.value.subscription_status in both modes now.
function accountHealth(m) {
	if (!m) return { level: "ok" };
	// Config changed but not yet (re)applied, or the last save is still being applied -
	// the last probe result no longer describes what's about to be saved, so it can't
	// be asserted verbatim. dirtyAccountHealth (@/llm/pool.js) is what decides how that
	// downgrades a settled health into what the dot actually shows - see its doc for
	// why a settled "ok" gets its own "pending" treatment instead of collapsing into
	// the same grey a never-verified row shows (PR #410 review finding 2).
	return dirtyAccountHealth(settledAccountHealth(m), dirty.value || sync.value.pending);
}
// The health accountHealth() would show if the pool were clean and no apply were in
// flight - i.e. purely from the last real signal, with no regard for whether that
// signal still describes what's about to be saved. Split out so dirtyAccountHealth
// (@/llm/pool.js) has a settled value to compare "what did we last actually measure"
// against "is that measurement stale".
function settledAccountHealth(m) {
	if (m.credentialType !== "subscription") {
		// api-key rows carry a PER-MODEL verdict (contract 1.11 model_statuses), probed
		// in isolation, so each shows its own health instead of the presence-only "key
		// set" that once made a dead model look identical to a healthy one. Onboarding
		// only ever renders the accounts/subscription branch below (its single api-key
		// row has no "Connected accounts" chip list to hang a dot on), but this stays
		// mode-agnostic for whenever that changes.
		return apiKeyModelHealth(m, sync.value.model_statuses);
	}
	if (singleMode.value) {
		// Onboarding's one connected account skips the failover-list's multi-row
		// disambiguation below (there is only ever one row here) but must NOT inherit
		// its "no verdict yet -> quiet green" default: right after OAuth paste-back,
		// BEFORE "Start chatting" even runs save_llm_pool, sync.value is whatever the
		// LAST applied config's status was - typically nothing at all for a brand-new
		// tenant - so degrading that to green here is exactly the bug above. knownGood
		// stays false so green is earned only by an explicit "verified".
		return subscriptionAccountHealth(sync.value.subscription_status, {
			knownGood: false,
			warningDetail: firstWarningMessage(),
		});
	}
	// sync.subscription_status is POOL-WIDE, not per-row: the fleet probes the pool's
	// subscription credential and returns ONE verdict. Painting it on every subscription
	// row is only honest when there is exactly one -- with two, a single "unverified"
	// would flag the healthy row too, and a "verified" would vouch for a row that was
	// never probed. Attribute it only when it can only mean this row; otherwise stay
	// neutral rather than assert something we did not measure.
	const subRows = rows.value.filter((r) => r.credentialType === "subscription");
	if (subRows.length > 1) return { level: "neutral" };
	// knownGood defaults true here: an absent verdict on the settings editor can mean
	// an EXISTING, previously-working pool that a pre-1.11 fleet just didn't report on -
	// unlike onboarding, that has actually been proven to work before.
	return subscriptionAccountHealth(sync.value.subscription_status, {
		warningDetail: firstWarningMessage(),
	});
}
// Open the connect panel WITHOUT starting OAuth.
//
// "+ Connect account" / "+ Add account" / "Reconnect" used to call startConnect
// directly. startConnect opens the sign-in tab SYNCHRONOUSLY inside the click -- it
// has to, or the window.open after its await loses the user gesture and gets
// popup-blocked. The side effect was a jarring flow: the customer clicked "Connect
// account" and was thrown straight at ChatGPT, then came back to a panel telling them
// to "Open sign-in" -- an action they had already, involuntarily, taken.
//
// So these buttons now just REVEAL the two-step spine (the same one onboarding shows
// up front), and step 1's "Open sign-in" is what actually starts OAuth. The tab still
// opens inside that click, so the popup-blocker fix is preserved.
function openConnectPanel(m, reconnectIdx = null) {
	const carried = (m._connect && m._connect.pastedUrl) || "";
	m._connect = { ...blankConnect(), open: true, reconnectIdx, pastedUrl: carried };
}

async function startConnect(m, reconnectIdx = null, opts = {}) {
	if (!m._connect) m._connect = blankConnect();
	// Simplified editor hides the model field - make sure a subscription row always
	// carries a model id so the connect flow never dead-ends on an unfillable field.
	if (singleMode.value && m.credentialType === "subscription" && !(m.model || "").trim()) {
		m.model = defaultSubscriptionModel(m.upstream, subscriptionSuggestions.value);
	}
	if (!(m.model || "").trim()) {
		m._connect = {
			...blankConnect(),
			open: true,
			error: "Enter a model id before connecting an account.",
		};
		return;
	}
	// Carry any already-typed callback URL across the reset: re-opening sign-in
	// (e.g. Reconnect, or retrying after an error) must not wipe pasted text.
	m._connect = {
		...blankConnect(),
		open: true,
		loading: true,
		reconnectIdx,
		pastedUrl: m._connect.pastedUrl || "",
	};
	// Open the sign-in tab SYNCHRONOUSLY, inside this click, so the browser treats
	// it as user-initiated. A window.open() after the await below loses the user
	// gesture and gets popup-blocked, which is why "Open sign-in" used to need a
	// second click (the first only fetched the URL). We navigate this blank tab
	// once the authorize URL resolves; if it was blocked (win === null) the visible
	// "Open sign-in ↗" link is still there for the user to click manually.
	// opts.openTab === false is the "Copy link" path: it only needs the URL, so
	// suppress the tab rather than spawning one the user did not ask for.
	let win = null;
	if (opts.openTab !== false) {
		try {
			win = window.open("about:blank", "_blank");
			if (win) win.opener = null;
		} catch (e) {
			win = null;
		}
	}
	try {
		const provider = UPSTREAM_OAUTH_PROVIDER[m.upstream] || "OpenAI";
		const res = await api.beginPoolAccountSignin(provider, m.model.trim());
		// Backend returns an envelope: {ok:true, data:{nonce, authorize_url, …}} or
		// {ok:false, error:{code, message}}. Unwrap data; surface errors instead of
		// hanging on "Starting sign-in…".
		if (!res || res.ok === false) {
			m._connect.loading = false;
			m._connect.error =
				(res && res.error && res.error.message) || "Couldn't start sign-in. Try again.";
			if (win) win.close();
			return;
		}
		const d = res.data || {};
		m._connect.nonce = d.nonce;
		m._connect.loading = false;
		if (d.device_flow) {
			// Device-code (Kimi): no authorize URL, no paste. Show the user_code +
			// verification link, open the verification page, and poll for approval.
			m._connect.deviceFlow = true;
			m._connect.userCode = d.user_code || "";
			m._connect.verificationUri = d.verification_uri || d.verification_uri_complete || "";
			const openUrl = d.verification_uri_complete || d.verification_uri;
			if (win && openUrl) win.location.href = openUrl;
			else if (win) win.close();
			_pollDeviceConnect(m, Math.max(2, Number(d.interval) || 5));
		} else {
			m._connect.authorizeUrl = d.authorize_url;
			if (win && d.authorize_url) win.location.href = d.authorize_url;
			else if (win) win.close();
		}
	} catch (e) {
		m._connect.loading = false;
		m._connect.error = _err(e);
		if (win) win.close();
	}
}
// Poll a device-code (Kimi) sign-in until the user approves, then place the
// account (same contract as finishConnect's success path). Bails if the panel
// is closed/reset or a new sign-in rebinds the nonce.
async function _pollDeviceConnect(m, intervalSecs) {
	const nonce = m._connect && m._connect.nonce;
	if (!nonce) return;
	m._connect.polling = true;
	const tick = async () => {
		if (!m._connect || !m._connect.deviceFlow || m._connect.nonce !== nonce) return;
		let res;
		try {
			res = await api.pollPoolAccountSignin(nonce);
		} catch (e) {
			if (m._connect && m._connect.nonce === nonce) {
				m._connect.error = _err(e);
				m._connect.polling = false;
			}
			return;
		}
		if (!m._connect || m._connect.nonce !== nonce) return;
		if (!res || res.ok === false) {
			m._connect.error =
				(res && res.error && res.error.message) || "Sign-in failed. Start again.";
			m._connect.polling = false;
			return;
		}
		const d = res.data || {};
		if (d.status === "pending") {
			setTimeout(tick, intervalSecs * 1000);
			return;
		}
		m._connect.polling = false;
		await _placeConnectedAccount(m, d);
	};
	setTimeout(tick, intervalSecs * 1000);
}
// Shared account placement for both the paste-back (finishConnect) and
// device-code (_pollDeviceConnect) success paths.
async function _placeConnectedAccount(m, d) {
	if (!Array.isArray(m.accounts)) m.accounts = [];
	const acct = {
		upstream: m.upstream || "openai",
		account_ref: d.account_ref,
		label: d.label || d.account_email || d.account_ref,
		account_email: d.account_email || "",
		oauth_blob: d.oauth_blob || "",
		connected: true,
	};
	const ri = m._connect.reconnectIdx;
	// Device-code accounts (Kimi) carry NO email, so byEmail can't fold two
	// captures of the same account — to REFRESH an existing device account use its
	// per-slot Reconnect (sets reconnectIdx, replacing that slot); a generic
	// Connect always appends (intended for pooling a genuinely different account).
	const byEmail = acct.account_email
		? m.accounts.findIndex(
				(a) =>
					a.account_email &&
					a.account_email.toLowerCase() === acct.account_email.toLowerCase()
		  )
		: -1;
	if (ri != null && ri >= 0 && ri < m.accounts.length) {
		m.accounts.splice(ri, 1, acct);
		if (byEmail >= 0 && byEmail !== ri) m.accounts.splice(byEmail, 1);
	} else if (byEmail >= 0) m.accounts.splice(byEmail, 1, acct);
	else m.accounts.push(acct);
	m._connect = blankConnect();
	if (!props.footerless) await save();
}
async function finishConnect(m) {
	if (!m._connect || !m._connect.nonce) return;
	if (!(m._connect.pastedUrl || "").trim()) {
		m._connect.error = isCodeOnlyPaste(m.upstream)
			? "Paste the code you were shown."
			: "Paste the URL you were redirected to.";
		return;
	}
	m._connect.loading = true;
	m._connect.error = "";
	try {
		const res = await api.completePoolAccountSignin(
			m._connect.nonce,
			m._connect.pastedUrl.trim()
		);
		// Same {ok, data} envelope as begin - unwrap + surface errors.
		if (!res || res.ok === false) {
			m._connect.loading = false;
			m._connect.error =
				(res && res.error && res.error.message) ||
				(isCodeOnlyPaste(m.upstream)
					? "Couldn't connect the account. Check the pasted code and try again."
					: "Couldn't connect the account. Check the pasted URL and try again.");
			return;
		}
		// Place the (re)connected account. The backend mints a fresh account_ref on
		// every sign-in, so it can't be a dedupe key: a per-account Reconnect refreshes
		// that exact slot (reconnectIdx); otherwise fold onto an existing account with
		// the same email; otherwise append a new one. The just-minted OAuth blob lives
		// only in memory until the pool is saved, so _placeConnectedAccount persists
		// immediately (unless footerless onboarding, where the host CTA drives save).
		await _placeConnectedAccount(m, res.data || {});
	} catch (e) {
		m._connect.loading = false;
		m._connect.error = _err(e);
	}
}
function closeConnect(m) {
	m._connect = blankConnect();
}
function copyConnectUrl(m, url) {
	if (!url) return;
	// Capture the connect object we are flashing. Reconnect (and closeConnect)
	// REPLACE m._connect wholesale, so a timer that re-reads m._connect can clear
	// the "Copied ✓" of a LATER, unrelated copy that started inside our 1400ms.
	const c = m._connect;
	copyTextWithFallback(url)
		.then(() => {
			c.copied = true;
			setTimeout(() => {
				if (m._connect === c) c.copied = false;
			}, 1400);
		})
		.catch(() => {
			c.error = "Could not copy. Select the URL above and copy manually.";
		});
}
function copyAuthorizeUrl(m) {
	copyConnectUrl(m, m._connect && m._connect.authorizeUrl);
}
// "Copy link" is offered from the START of the sign-in step, not only after
// "Open sign-in" has already fetched a URL. Signing in on a PHONE (or any second
// device) is the whole point of copying, and forcing a tab open on this machine
// first was a pointless detour. When no authorize URL exists yet we fetch one on
// demand with the tab suppressed, then copy it.
async function copySigninLink(m) {
	if (m._connect && m._connect.authorizeUrl) {
		copyAuthorizeUrl(m);
		return;
	}
	const reconnectIdx = (m._connect && m._connect.reconnectIdx) ?? null;
	await startConnect(m, reconnectIdx, { openTab: false });
	if (!m._connect) return;
	if (m._connect.authorizeUrl) {
		copyAuthorizeUrl(m);
		return;
	}
	// Device-code upstreams (Kimi) resolve a VERIFICATION PAGE instead of an
	// authorize URL, and the panel flips to the code step underneath this click.
	// That page is exactly the link worth carrying to a second device, so copy it
	// rather than leaving the click silently doing nothing.
	if (m._connect.deviceFlow) {
		if (m._connect.verificationUri) copyConnectUrl(m, m._connect.verificationUri);
		return;
	}
	// No URL and startConnect surfaced no error: say so rather than dead-clicking.
	if (!m._connect.error) {
		m._connect.error = "Couldn't get a sign-in link. Try Open sign-in instead.";
	}
}

// ---- load / save ---------------------------------------------------------
// Seed the canonical rows from get_llm_config, then augment each with the
// transient UI-only fields the editor needs (upstream + _connect). Seeded
// accounts carry no oauth_blob (never returned by the server) - reconnect to
// change; they render as "connected" via their label.
function seedRows(config) {
	return seedRowsFromConfig(config).map((r) => {
		const upstream = (r.accounts && r.accounts[0] && r.accounts[0].upstream) || "openai";
		// Backfill a missing model id on a STORED subscription row.
		//
		// The subscription model field was removed from both editors: the id is derived
		// in setCredType/onUpstreamChange. But neither of those fires for a row that
		// merely LOADS from get_llm_config -- so a stored row with an empty model (legacy
		// data, or a pool written before the id was required) would render "Model not set"
		// with NO field to type one into, and every Save would then fail validatePool's
		// "Every model needs a model id" with no way out. Derive it here, on the same rule
		// the editors use, so such a row is repairable instead of permanently stuck.
		const model =
			r.credentialType === "subscription" && !(r.model || "").trim()
				? defaultSubscriptionModel(upstream, subscriptionSuggestions.value)
				: r.model;
		return {
			...r,
			_uid: nextUid(),
			model,
			upstream,
			accounts: (r.accounts || []).map((a) => ({ ...a, oauth_blob: "" })),
			_connect: blankConnect(),
		};
	});
}

async function load() {
	err.value = "";
	try {
		cfg.value = (await api.getLlmConfig()) || cfg.value;
		rows.value = seedRows(cfg.value);
		selectedPreset.value = cfg.value.preset || "";
		keysByVendor.value = {};
		// Open on the tab that matches what's stored (mirrors seedLlmSetupFromConfig).
		if (selectedPreset.value) llmMode.value = "preset";
		else if (
			rows.value.length >= 2 ||
			rows.value.some((r) => r.credentialType === "subscription")
		)
			llmMode.value = "custom";
		else {
			llmMode.value = "quick";
			if (!rows.value.length) rows.value = [newRow()];
		}
		// Onboarding is quick-only (singleMode): the editor shows a single editable
		// row (editorRows renders rows[0]) but we KEEP any seeded tail rows so a
		// returning customer's existing failover pool round-trips through save()
		// instead of being silently dropped. Only the preset (which quick can't
		// represent) is cleared.
		if (singleMode.value) {
			llmMode.value = props.modes[0] || "quick";
			selectedPreset.value = "";
			if (!rows.value.length) rows.value = [newRow()];
			// Onboarding default = Chat subscription (the common path). Only flip a
			// pristine row so a returning customer's saved API-key setup is preserved.
			const r0 = rows.value[0];
			if (
				r0 &&
				r0.credentialType === "api_key" &&
				!(r0.model || "").trim() &&
				!(r0.apiKey || "").trim() &&
				!r0.hasKey &&
				!(r0.accounts && r0.accounts.length)
			) {
				setCredType(r0, "subscription");
			}
		} else {
			// Settings (!singleMode) editor: always the unified failover-list view -
			// Quick/Preset tabs are gone, "From a preset" now lives inside the
			// config-section add flow (seedFromPreset/selectPreset), so llmMode never
			// needs to be "preset" or "quick" here.
			llmMode.value = "custom";
			selectedPreset.value = "";
			if (!rows.value.length) rows.value = [newRow()];
		}
		// Baseline for the unsaved-changes notice - the pool as just loaded is clean.
		savedSnapshot.value = poolSnapshot();
	} catch (e) {
		err.value = _err(e);
	}
	// A sync may already be in flight when the editor mounts (page reload
	// mid-provisioning, wizard resume via reason llm_pool_provisioning): start
	// the poller for a pending one - polling only from save() left a resumed
	// session staring at a permanent "Syncing…" banner that never picked up
	// the background job's ok/failed flip.
	try {
		sync.value = (await api.getLlmSyncStatus()) || sync.value;
		if (sync.value && sync.value.pending) startPolling();
	} catch (e) {
		/* non-fatal */
	}
	try {
		catalog.value = (await api.getPresetCatalog()) || [];
	} catch (e) {
		/* backend bundled fallback */
	}
	try {
		modelCatalog.value = (await api.getModelCatalogUi()) || modelCatalog.value;
	} catch (e) {
		/* built-in literal fallbacks below cover this */
	}
}

// Stable string of the savable pool + preset - the cheap key the dirty-notice
// and snapshot reset compare against.
function poolSnapshot() {
	try {
		return JSON.stringify({ m: buildSaveModels(rows.value), p: selectedPreset.value });
	} catch (e) {
		return "";
	}
}

// Build the per-row backend shape save_llm_pool expects (matches AiView + desk).
function buildSaveModels(sourceRows) {
	return (sourceRows || []).map((r, i) => {
		if (r.credentialType === "subscription") {
			return {
				model: (r.model || "").trim(),
				order: i,
				subscription: {
					rotation: r.rotation || "sticky",
					accounts: (r.accounts || []).map((a) => ({
						upstream: a.upstream || "openai",
						account_ref: a.account_ref,
						label: a.label,
						oauth_blob: a.oauth_blob || "",
					})),
				},
			};
		}
		const m = {
			provider: (r.provider || "").trim(),
			model: (r.model || "").trim(),
			api_key: effectiveApiKey(r.provider, r.apiKey, r.hasKey),
			order: i,
		};
		if (r.hasKey) m.has_key = true; // let validatePool + backend merge keep a stored key on re-save
		const b = (r.baseUrl || "").trim();
		if (b) m.base_url = b;
		return m;
	});
}

// A blank API-KEY row is a placeholder, not a choice: load() seeds one into an EMPTY
// pool so there is something to fill in, and an abandoned "+ Add a model" can leave
// one behind. It must not block a Save whose other rows are real -- validatePool
// rejects the whole pool with "Every model needs a provider and a model id", which
// names nothing the customer recognises and points at no row. Dropping it loses
// nothing the customer chose.
//
// Deliberately narrow: an in-progress SUBSCRIPTION row (connect started, no account
// yet) is NOT pruned. That row represents intent, so validation should say "connect
// your account" rather than let it silently vanish. And if pruning would empty the
// pool we keep every row, so validation still speaks up instead of saving nothing.
function prunedForSave(src) {
	const all = src || [];
	const kept = all.filter((r) => !(r.credentialType !== "subscription" && isRowEmpty(r)));
	return kept.length ? kept : all;
}

async function save() {
	err.value = "";
	let saveModels, savePreset;
	if (llmMode.value === "preset") {
		const e = selectedEntry.value;
		if (!e) {
			err.value = "Pick a preset.";
			return;
		}
		saveModels = presetToModels(e, keysByVendor.value);
		savePreset = selectedPreset.value || null;
	} else {
		// Quick saves a single-model pool (rows[0]); Custom saves the full pool.
		// Exception: onboarding's singleMode keeps seeded tail rows (editorRows only
		// renders the first) so a returning customer's existing pool isn't dropped.
		const src = prunedForSave(rows.value);
		saveModels = buildSaveModels(
			llmMode.value === "quick" && !singleMode.value ? src.slice(0, 1) : src
		);
		savePreset = null;
	}
	// Simplified editor hides the model id, so validatePool's "Model <id> needs a
	// connected account" would name a value the user never saw. Pre-check with a
	// clear message instead.
	if (singleMode.value && llmMode.value !== "preset") {
		const r0 = rows.value[0];
		if (
			r0 &&
			r0.credentialType === "subscription" &&
			!(r0.accounts || []).some((a) => a && (a.oauth_blob || a.account_ref))
		) {
			err.value = "Connect your account to continue.";
			return;
		}
	}
	const v = validatePool(saveModels, savePreset);
	if (!v.ok) {
		err.value = v.error;
		return;
	}
	saving.value = true;
	try {
		await api.saveLlmPool(saveModels, savePreset, "failover");
		try {
			sync.value = await api.getLlmSyncStatus();
		} catch (e) {
			/* keep prior */
		}
		startPolling();
		emit("saved", sync.value);
		await load();
	} catch (e) {
		err.value = _err(e);
	} finally {
		saving.value = false;
	}
}

function startPolling() {
	stopPolling();
	pollTimer = setInterval(async () => {
		try {
			sync.value = await api.getLlmSyncStatus();
			if (!sync.value.pending) stopPolling();
		} catch (e) {
			stopPolling();
		}
	}, 3000);
}
function stopPolling() {
	if (pollTimer) {
		clearInterval(pollTimer);
		pollTimer = null;
	}
}

// Refresh the preset preview whenever vendor keys change while a preset is active.
watch(
	keysByVendor,
	() => {
		if (llmMode.value === "preset" && selectedPreset.value) {
			const e = selectedEntry.value;
			if (e) rows.value = seedFromPreset(e);
		}
	},
	{ deep: true }
);

// Tell the host (onboarding footer) when the config becomes savable so it can
// highlight the "Onboard Jarvis" CTA.
watch(ready, (v) => emit("ready", v), { immediate: true });

onMounted(load);
onBeforeUnmount(stopPolling);

// Let a host (onboarding, footerless) drive Save from its own footer.
defineExpose({ save });
</script>

<style scoped>
/* ===== Account editor (!singleMode) row redesign - "Option A: refine in
   place". Onboarding's singleMode cards below are untouched; these jv-pool-*
   classes are new and only ever rendered from the !singleMode branches. ===== */
.jv-pool-rowhead {
	display: flex;
	align-items: center;
	gap: 8px;
	margin-bottom: 8px;
}
/* 1-based failover-order badge. */
.jv-pool-badge {
	flex: none;
	width: 22px;
	height: 22px;
	border-radius: 6px;
	background: var(--cta-bg);
	color: var(--cta);
	font-size: 11.5px;
	font-weight: 700;
	display: grid;
	place-items: center;
}
/* Credential-type segmented control (replaces the old pale text-pair toggle). */
.jv-pool-segct {
	display: inline-flex;
	border: 1px solid var(--border-2);
	border-radius: 8px;
	overflow: hidden;
}
.jv-pool-segbtn {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	height: 31px;
	padding: 0 11px;
	border: none;
	border-right: 1px solid var(--border-2);
	background: var(--surface);
	color: var(--text-2);
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 500;
	cursor: pointer;
	transition: background 0.15s, color 0.15s;
}
.jv-pool-segbtn:last-child {
	border-right: none;
}
.jv-pool-segbtn svg {
	flex: none;
}
.jv-pool-segbtn.on {
	background: var(--text);
	color: var(--surface);
	font-weight: 600;
}
.jv-pool-segbtn:disabled {
	cursor: default;
	opacity: 0.6;
}
/* Reorder / remove icon buttons (replace the ▲/▼/✕ glyph squares). */
.jv-pool-iconbtn {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	width: 28px;
	height: 28px;
	border-radius: 6px;
	padding: 0;
	border: 1px solid var(--border);
	background: var(--surface);
	color: var(--text-2);
	cursor: pointer;
	transition: background 0.15s, color 0.15s;
}
.jv-pool-iconbtn:hover:not(:disabled) {
	background: var(--surface-2);
	color: var(--text);
}
.jv-pool-iconbtn:disabled {
	cursor: default;
	opacity: 0.4;
}
.jv-pool-iconbtn--danger {
	border-color: var(--red-bd);
	background: var(--red-bg);
	color: var(--red);
}
.jv-pool-iconbtn--danger:hover:not(:disabled) {
	background: var(--red-bg);
	color: var(--red);
}
/* Labeled field columns (Provider / Model / API key / Base URL, Model /
   Provider / Account rotation). Flex proportions stay on this wrapper - the
   input/select inside just fills width:100%. */
.jv-pool-field {
	display: flex;
	flex-direction: column;
	min-width: 0;
}
.jv-pool-lab {
	font-size: 10.5px;
	font-weight: 600;
	text-transform: uppercase;
	letter-spacing: 0.03em;
	color: var(--text-3);
	margin-bottom: 3px;
}
/* Connected-accounts chip list. */
.jv-pool-accts {
	margin-top: 16px;
	margin-bottom: 8px;
}
.jv-pool-accts > .jv-pool-lab {
	margin-bottom: 6px;
}
.jv-pool-acctlist {
	display: flex;
	flex-direction: column;
	gap: 6px;
}
.jv-pool-acctchip {
	display: flex;
	align-items: center;
	gap: 8px;
	border: 1px solid var(--border);
	background: var(--surface);
	border-radius: 8px;
	padding: 7px 10px;
}
.jv-pool-avatar {
	flex: none;
	width: 22px;
	height: 22px;
	border-radius: 50%;
	background: var(--cta-bg);
	color: var(--cta);
	font-size: 10.5px;
	font-weight: 700;
	display: grid;
	place-items: center;
}
.jv-pool-accttx {
	font-size: 12.5px;
	color: var(--text);
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-pool-dot {
	flex: none;
	width: 7px;
	height: 7px;
	border-radius: 50%;
	background: var(--green);
}
/* Option A "honest model health" - dot color reflects the fleet's last
   subscription probe. --neutral now shares --unchecked's grey, NOT --ok's green:
   "nothing known yet" and "not verified yet" are the same customer-facing state, and
   painting neutral green is exactly how an unverified, out-of-quota account got shown
   as healthy before anyone had checked it (2026-07-23 trace). Green is reserved for an
   explicit --ok verdict only. */
.jv-pool-dot--ok {
	background: var(--green);
}
.jv-pool-dot--warn {
	background: var(--amber);
}
.jv-pool-dot--neutral,
.jv-pool-dot--unchecked {
	background: var(--text-3);
}
/* A settled "ok" row caught mid-edit or mid-apply (accountHealth's dirty/pending
   branch) - deliberately NOT --text-3 grey. Sharing that colour with --neutral would
   make "previously verified, about to be re-checked" indistinguishable from "never
   verified at all", which is the exact regression this rule exists to avoid (PR #410
   review finding 2). --link is the one sanctioned "in progress" blue elsewhere in this
   app (see theme.js) - calm, not alarming, and visibly not the unproven grey. */
.jv-pool-dot--pending {
	background: var(--link);
}
/* flex: none + a cap, not 0 1 auto: the label can now carry a provider's own error detail
   (apiKeyModelHealth's `detail`, e.g. a GLM/Z.ai balance message) instead of always being one
   of two fixed short strings - pool.js already truncates the text itself, this is just a
   layout safety net so a row can never overflow. */
.jv-pool-acct-health {
	flex: none;
	max-width: 220px;
	overflow: hidden;
	text-overflow: ellipsis;
	font-size: 12px;
	font-weight: 600;
	white-space: nowrap;
}
.jv-pool-acct-health--warn {
	color: var(--amber);
}
.jv-pool-acct-health--unchecked {
	color: var(--text-3);
}
.jv-pool-acct-health--pending {
	color: var(--link);
}
.jv-pool-acctacts {
	margin-left: auto;
	display: flex;
	gap: 6px;
	flex: none;
}
.jv-pool-disc {
	color: var(--red);
}
.jv-pool-disc:hover:not(:disabled) {
	color: var(--red);
	border-color: var(--red-bd);
	background: var(--red-bg);
}
/* Full-width dashed "+ Add account" row appended to a non-empty chip list. */
.jv-pool-addrow {
	display: flex;
	align-items: center;
	justify-content: center;
	width: 100%;
	height: 32px;
	border-radius: 8px;
	border: 1px dashed var(--border-2);
	background: transparent;
	color: var(--text-2);
	font-family: inherit;
	font-size: 12px;
	font-weight: 600;
	cursor: pointer;
	transition: background 0.15s, color 0.15s;
}
.jv-pool-addrow:hover:not(:disabled) {
	background: var(--surface-2);
	color: var(--text);
}
.jv-pool-addrow:disabled {
	opacity: 0.5;
	cursor: default;
}
/* Save-bar apply-status pill (Option A "honest model health") - reflects the
   outcome of the last apply once there are no unsaved edits sitting on top
   of it. Same quiet weight as the rest of this settings UI. */
.jv-pool-syncpill {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	font-size: 12.5px;
	font-weight: 600;
	padding: 3px 10px;
	border-radius: 999px;
	border: 1px solid transparent;
}
.jv-pool-syncpill-ic {
	flex: none;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	width: 10px;
}
.jv-pool-syncpill--ok {
	color: var(--green);
	background: var(--green-bg);
	border-color: var(--green-bd);
}
.jv-pool-syncpill--ok .jv-pool-syncpill-ic::before {
	content: "✓";
}
.jv-pool-syncpill--warn {
	color: var(--amber);
	background: var(--amber-bg);
	border-color: var(--amber-bd);
}
.jv-pool-syncpill--warn .jv-pool-syncpill-ic::before {
	content: "⚠";
}
.jv-pool-syncpill--failed {
	color: var(--red);
	background: var(--red-bg);
	border-color: var(--red-bd);
}
.jv-pool-syncpill--failed .jv-pool-syncpill-ic::before {
	content: "⚠";
}
.jv-pool-syncpill--pending {
	color: var(--text-3);
	background: transparent;
}
.jv-pool-syncpill--pending .jv-pool-syncpill-ic {
	width: 6px;
	height: 6px;
	border-radius: 50%;
	background: var(--text-3);
	animation: jv-pool-pulse 1.1s ease-in-out infinite;
}
@keyframes jv-pool-pulse {
	0%,
	100% {
		opacity: 0.35;
	}
	50% {
		opacity: 1;
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-pool-segbtn,
	.jv-pool-iconbtn,
	.jv-pool-addrow {
		transition: none;
	}
	.jv-pool-syncpill--pending .jv-pool-syncpill-ic {
		animation: none;
		opacity: 0.7;
	}
}

/* Unified failover list row (!singleMode only) - order badge, source chip,
   model id, health dot, RIGHT-ALIGNED actions cluster. */
.jv-flist-row {
	display: flex;
	align-items: center;
	gap: 9px;
	flex-wrap: wrap;
	border: 1px solid var(--border);
	border-radius: 9px;
	padding: 9px 11px;
	margin-bottom: 8px;
	background: var(--surface-1);
	transition: border-color 0.15s;
}
.jv-flist-row:hover {
	border-color: var(--border-2);
}
@media (prefers-reduced-motion: reduce) {
	.jv-flist-row {
		transition: none;
	}
}
.jv-flist-chip {
	flex: none;
	font-size: 11.5px;
	font-weight: 600;
	color: var(--text-2);
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 999px;
	padding: 3px 9px;
	white-space: nowrap;
}
.jv-flist-model {
	font-size: 13.5px;
	color: var(--text);
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-flist-acts {
	margin-left: auto;
	display: flex;
	gap: 6px;
	align-items: center;
	flex: none;
}
/* An unset model reads as a placeholder, not as a real model id. */
.jv-flist-model--unset {
	color: var(--text-3);
	font-style: italic;
}

/* ---- explainer + add affordance + failover nudge (settings editor only) ----
   Flat neutral surfaces, monochrome accent, no decorative colour: the only hue in
   this pane stays semantic (the green Applied pill, the red Remove). */
/* ---- config panel fields -----------------------------------------------
   One grid + one input class, replacing per-field inline styles and flex ratios
   (1 / 1.5 / 1.5 / 1.5) that gave every field a different width. Two even columns
   read as a form; four uneven ones read as clutter. */
.jv-cfg-grid {
	display: grid;
	grid-template-columns: 1fr 1fr;
	gap: 12px 14px;
	align-items: end;
}
/* The dropdowns are JvCombo (the app's own), NOT native <select> — a native one is
   drawn by the OS with its own popup, which is exactly why this panel used to look
   nothing like onboarding. These metrics keep the plain inputs (API key, Base URL)
   dimensionally identical to a .jvc-field so the grid reads as ONE control set. */
.jv-cfg-inp,
.jv-cfg-grid :deep(.jvc-field) {
	width: 100%;
	min-height: 40px;
	padding: 9px 12px;
	font-size: 14px;
	font-family: inherit;
	border: 1px solid var(--border);
	border-radius: 8px;
	background: var(--surface);
	color: var(--text);
	box-sizing: border-box;
	transition: border-color 0.15s ease;
}
.jv-cfg-inp:hover:not(:disabled),
.jv-cfg-grid :deep(.jvc-field:hover) {
	border-color: var(--border-2);
}
.jv-cfg-inp:focus {
	outline: none;
	border-color: var(--text-3);
	box-shadow: none;
}
.jv-cfg-grid :deep(.jvc-field:focus-within) {
	border-color: var(--text-3);
}
.jv-cfg-inp:disabled {
	opacity: 0.6;
	cursor: default;
}
.jv-pool-opt {
	font-weight: 400;
	color: var(--text-3);
}
@media (max-width: 720px) {
	.jv-cfg-grid {
		grid-template-columns: 1fr;
	}
}

/* "Soon" tag on the not-yet-shipped preset tab. */
.jv-pool-segbtn--soon {
	cursor: default;
	opacity: 0.55;
}
.jv-soon {
	margin-left: 6px;
	padding: 1px 6px;
	border-radius: 20px;
	background: var(--surface-3);
	color: var(--text-3);
	font-size: 10px;
	font-weight: 600;
	letter-spacing: 0.02em;
	text-transform: uppercase;
}

/* "+ Add a model" is a .jv-btn (see the template note): only its spacing and the
   plus glyph's tint are local. Everything else — height, radius, type, hover — comes
   from the shared button system, so it can never drift from Edit / Remove / Save. */
.jv-flist-addbtn {
	margin-top: 10px;
	gap: 6px;
}
/* "Add a model" trails the failover LIST, so it sits at the list's right edge --
   the same edge Save configuration occupies, which is where the eye already is
   after reading the rows. Scoped to a modifier because .jv-flist-addbtn is shared
   with the panel's "Connect account", which stays left-aligned under its field.
   display/width are needed because the parent <section> is a plain block: an
   inline-flex .jv-btn ignores margin-left:auto until it is block-level. */
.jv-flist-addbtn--end {
	display: flex;
	width: fit-content;
	margin-left: auto;
}
.jv-flist-addbtn svg {
	color: var(--text-3);
	flex: none;
}
.jv-flist-addbtn:hover:not(:disabled) svg {
	color: var(--text);
}
/* Consequence-first nudge shown while the pool has no fallback. */
.jv-flist-hint {
	display: flex;
	align-items: flex-start;
	gap: 9px;
	margin-top: 14px;
	padding: 11px 13px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface-1);
	font-size: 12.5px;
	line-height: 1.55;
	color: var(--text-2);
}
.jv-flist-hint svg {
	flex: none;
	margin-top: 1px;
	color: var(--text-3);
}
.jv-flist-hint b {
	color: var(--text);
	font-weight: 600;
}
/* Master-detail config section (add/edit a row, or apply a preset). */
.jv-cfgpanel {
	border: 1px solid var(--border-2);
	border-radius: 11px;
	padding: 14px;
	margin: 4px 0 14px;
	background: var(--surface);
}
.jv-cfgpanel-head {
	display: flex;
	align-items: center;
	justify-content: space-between;
	margin-bottom: 10px;
}
.jv-cfgpanel-title {
	font-size: 13.5px;
	font-weight: 700;
	color: var(--text);
}
.jv-cfgpanel-acts {
	display: flex;
	justify-content: flex-end;
	gap: 8px;
	margin-top: 14px;
}

/* Onboarding method cards (preview .method/.m-opt): sel = blue border + 3px
   ring; icon tile flips from neutral to blue tint when selected. Preview's
   --accent maps to the app's --cta (and -bg/-bd). */
.jv-ct {
	margin-bottom: 20px;
}
.jv-ct-cards {
	display: grid;
	grid-template-columns: 1fr 1fr;
	gap: 12px;
}
.jv-ct-card {
	display: flex;
	align-items: flex-start;
	gap: 12px;
	text-align: left;
	padding: 15px 16px;
	border: 1.5px solid var(--border);
	border-radius: 12px;
	background: var(--surface);
	cursor: pointer;
	font: inherit;
	color: var(--text);
	transition: border-color 0.15s, box-shadow 0.15s;
}
.jv-ct-card.on {
	border-color: var(--cta);
	box-shadow: 0 0 0 3px var(--cta-bg);
}
.jv-ct-card:disabled {
	cursor: default;
}
.jv-ct-ic {
	flex: none;
	width: 34px;
	height: 34px;
	border-radius: 9px;
	display: grid;
	place-items: center;
	background: var(--surface-2);
	border: 1px solid var(--border);
	color: var(--text-2);
}
.jv-ct-card.on .jv-ct-ic {
	background: var(--cta-bg);
	border-color: var(--cta-bd);
	color: var(--cta);
}
.jv-ct-ic svg {
	width: 17px;
	height: 17px;
	stroke-width: 1.8;
}
.jv-ct-tx {
	display: flex;
	flex-direction: column;
	gap: 3px;
	min-width: 0;
}
.jv-ct-t {
	font-size: 13.5px;
	font-weight: 600;
}
.jv-ct-d {
	font-size: 12px;
	color: var(--text-3);
	line-height: 1.4;
}
/* Labeled compact "Provider & model" select (preview .fieldlab/.sel-provider):
   40px field, 10px radius, border-2 border, same 3px focus ring as the rest of
   the wizard's inputs. */
.jv-fieldlab {
	font-size: 12px;
	font-weight: 550;
	color: var(--text-2);
	margin-bottom: 6px;
}
.jv-pick :deep(.jvc-field) {
	min-height: 40px;
	padding: 0 14px;
	border-color: var(--border-2);
	border-radius: 10px;
	font-size: 13.5px;
	transition: border-color 0.15s, box-shadow 0.15s;
}
.jv-pick :deep(.jvc-field:hover) {
	border-color: var(--border-2);
}
.jv-pick :deep(.jvc-field:focus-within),
.jv-pick :deep(.jvc-field.jvc-open) {
	border-color: var(--border-2);
}
/* The two connect steps on a connected vertical spine (preview .csteps): no
   shade boxes, a 1.5px line joins the numbered dots; step 2 reads pending
   (neutral dot) until the sign-in URL exists. */
.jv-cdivider {
	height: 1px;
	background: var(--border);
	margin: 20px 0 18px;
}
.jv-cstep {
	position: relative;
	display: flex;
	gap: 14px;
	padding: 2px 0 22px;
}
.jv-cstep:last-child {
	padding-bottom: 4px;
}
.jv-cstep:not(:last-child)::before {
	content: "";
	position: absolute;
	left: 12.5px;
	top: 32px;
	bottom: 4px;
	width: 1.5px;
	background: var(--border);
}
.jv-cnum {
	width: 26px;
	height: 26px;
	border-radius: 50%;
	box-sizing: border-box;
	background: var(--text);
	color: var(--surface);
	display: grid;
	place-items: center;
	font-size: 12.5px;
	font-weight: 600;
	flex: none;
	position: relative;
	z-index: 1;
}
.jv-cstep.jv-pending .jv-cnum {
	background: var(--surface-2);
	color: var(--text-3);
	border: 1.5px solid var(--border-2);
}
.jv-cbody {
	flex: 1;
	min-width: 0;
}
.jv-ctit {
	font-size: 13.5px;
	font-weight: 600;
	margin-bottom: 3px;
}
/* Step-1 header row: title left, sign-in action(s) right on the same line. */
.jv-chead {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 12px;
	flex-wrap: wrap;
	margin-bottom: 6px;
}
.jv-chead .jv-ctit {
	margin-bottom: 0;
}
.jv-cdesc {
	font-size: 12.5px;
	color: var(--text-3);
	line-height: 1.45;
	margin-bottom: 0;
}
.jv-crow {
	display: flex;
	justify-content: flex-end;
	gap: 9px;
	flex-wrap: wrap;
}
/* Small in-step buttons (preview .btn--sm on .btn--primary/.btn--ghost). */
.jv-cbtn {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	gap: 7px;
	height: 34px;
	padding: 0 13px;
	border-radius: 9px;
	border: 1px solid transparent;
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 600;
	line-height: 1;
	cursor: pointer;
	white-space: nowrap;
	text-decoration: none;
	transition: transform 0.12s, box-shadow 0.15s, background 0.15s, border-color 0.15s;
}
.jv-cbtn:active {
	transform: scale(0.98);
}
/* The Open sign-in control is an <a>, which the wizard's button-scoped
   focus-visible rule misses - give both spine controls their own outline. */
.jv-cbtn:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
.jv-cbtn:disabled {
	opacity: 0.55;
	cursor: default;
	transform: none;
}
.jv-cbtn-primary {
	background: var(--text);
	color: var(--surface);
	box-shadow: 0 2px 10px rgba(20, 20, 30, 0.16);
}
.jv-cbtn-primary:hover:not(:disabled) {
	color: var(--surface);
	transform: translateY(-1px);
	box-shadow: 0 8px 22px rgba(20, 20, 30, 0.22);
}
.jv-cbtn-ghost {
	background: var(--surface);
	border-color: var(--border-2);
	color: var(--text-2);
}
.jv-cbtn-ghost:hover:not(:disabled) {
	background: var(--surface-2);
	color: var(--text);
	border-color: var(--border);
}
/* ONE amber callout: the "This site can't be reached is expected" guidance with
   the inline kbd shortcut hint (preview .callout). */
.jv-callout {
	display: flex;
	gap: 9px;
	align-items: flex-start;
	background: var(--amber-bg);
	border: 1px solid var(--amber-bd);
	border-radius: 9px;
	padding: 9px 12px;
	margin-bottom: 10px;
}
.jv-callout svg {
	color: var(--amber);
	flex: none;
	margin-top: 1px;
}
.jv-callout p {
	margin: 0;
	font-size: 12px;
	color: var(--text-2);
	line-height: 1.5;
}
.jv-callout b {
	color: var(--text);
	font-weight: 600;
}
.jv-callout kbd {
	font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	font-size: 10px;
	background: var(--surface);
	border: 1px solid var(--amber-bd);
	border-radius: 4px;
	padding: 0 4px;
}
/* Dashed mono paste input; focus solidifies the border + shows the wizard's
   3px ring (preview .paste). */
.jv-paste {
	width: 100%;
	height: 44px;
	box-sizing: border-box;
	border: 1.5px dashed var(--border-2);
	border-radius: 11px;
	background: var(--surface-1);
	padding: 0 14px;
	font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	font-size: 12.5px;
	color: var(--text);
	transition: border-color 0.15s, background 0.15s;
}
.jv-paste::placeholder {
	color: var(--text-3);
}
.jv-paste:focus {
	outline: none;
	border-style: solid;
	border-color: var(--cta);
	background: var(--surface);
	box-shadow: 0 0 0 3px var(--cta-bg);
}
.jv-paste:disabled {
	opacity: 0.55;
}
.jv-cacts {
	display: flex;
	justify-content: flex-end;
	gap: 8px;
	margin-top: 10px;
}
/* Clean status pill - connected (ok) / failed (bad). Reused by the subscription
   connected row and (later) the API-key verify result. No red ✕ - a subtle text
   action handles disconnect. */
.jv-status {
	display: flex;
	align-items: center;
	gap: 9px;
	padding: 10px 12px;
	border-radius: 9px;
	font-size: 13.5px;
	margin-bottom: 8px;
}
.jv-status-ok {
	border: 1px solid var(--green-bd);
	background: var(--green-bg);
}
.jv-status-bad {
	border: 1px solid var(--red-bd);
	background: var(--red-bg);
}
.jv-status-ic {
	flex: none;
	display: flex;
	color: var(--green);
}
.jv-status-bad .jv-status-ic {
	color: var(--red);
}
.jv-status-tx {
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	color: var(--text);
}
.jv-status-tx b {
	color: var(--green);
	font-weight: 600;
}
.jv-status-bad .jv-status-tx b {
	color: var(--red);
}
.jv-status-acts {
	margin-left: auto;
	display: flex;
	gap: 12px;
	flex: none;
}
.jv-status-act {
	background: transparent;
	border: 0;
	color: var(--text-3);
	font-size: 12.5px;
	cursor: pointer;
	padding: 0;
}
.jv-status-act:hover {
	color: var(--text);
	text-decoration: underline;
	text-underline-offset: 2px;
}
/* Paste-back OAuth connect panel - two numbered steps (open sign-in URL / paste
   the callback URL), styled to match the rest of the onboarding editor. */
.jv-cn-acts {
	display: flex;
	justify-content: flex-end;
	gap: 8px;
}
/* The old .jv-cn* connect panel is GONE: settings now renders the same .jv-csteps
   spine as onboarding, so there is one connect flow, not two that drift apart.
   Only -err and -acts survive (still used by that spine). */
.jv-cn-err {
	margin-top: 9px;
	font-size: 13px;
	color: var(--red);
}
/* Lock both credential modes to the same body height in onboarding so toggling
   API key ↔ Chat subscription never resizes the card (first-impression polish). */
.jv-single-body {
	min-height: 96px;
}
/* API-key fields as a 2×2 grid in onboarding - keeps this view's height close to
   the subscription view so toggling doesn't resize the card. Fields match the
   wizard's inputs (42px, 10px radius, border-2, 3px focus ring). */
.jv-ak-grid {
	display: grid;
	grid-template-columns: 1fr 1fr;
	gap: 12px;
}
.jv-ak-grid input {
	width: 100%;
	height: 42px;
	padding: 0 13px;
	font-size: 13.5px;
	border: 1px solid var(--border-2);
	border-radius: 10px;
	background: var(--surface);
	color: var(--text);
	font-family: inherit;
	box-sizing: border-box;
}
.jv-ak-grid input::placeholder {
	color: var(--text-3);
}
.jv-ak-grid input:focus {
	outline: none;
	border-color: var(--cta);
	box-shadow: 0 0 0 3px var(--cta-bg);
}
.jv-ak-grid :deep(.jvc-field) {
	min-height: 42px;
	padding: 0 13px;
	border-color: var(--border-2);
	border-radius: 10px;
	font-size: 13.5px;
	transition: border-color 0.15s, box-shadow 0.15s;
}
.jv-ak-grid :deep(.jvc-field:hover) {
	border-color: var(--border-2);
}
.jv-ak-grid :deep(.jvc-field:focus-within),
.jv-ak-grid :deep(.jvc-field.jvc-open) {
	border-color: var(--border-2);
}
.jv-ak-grid :deep(.jvc-input::placeholder) {
	color: var(--text-3);
}
/* Preview stacks .method at 820px - the same breakpoint as the wizard's other grids. */
@media (max-width: 820px) {
	.jv-ct-cards,
	.jv-ak-grid {
		grid-template-columns: 1fr;
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-ct-card,
	.jv-cbtn,
	.jv-paste,
	.jv-pick :deep(.jvc-field),
	.jv-ak-grid :deep(.jvc-field) {
		transition: none;
	}
}
.jv-mon-retry {
	display: inline-flex;
	align-items: center;
	margin-left: 6px;
	height: 24px;
	border: none;
	background: var(--surface-2);
	color: var(--text);
	border-radius: 8px;
	padding: 0 10px;
	font-size: 12px;
	font-weight: 500;
	font-family: inherit;
	cursor: pointer;
	transition: background-color 0.15s ease;
}
.jv-mon-retry:hover {
	background: var(--surface-3);
}
</style>
