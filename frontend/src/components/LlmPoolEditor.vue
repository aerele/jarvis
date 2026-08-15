<template>
	<!-- Reusable LLM-pool editor. Renders the 3-mode SETUP UI (Quick | Preset |
       Custom) over the unified proxy-pool model and persists via saveLlmPool.
       Self-loads its config on mount (seeded through seedRowsFromConfig into the
       canonical camelCase row shape). Expects an ancestor to supply the theme
       CSS vars (--surface, --text, …); uses only tokens, no hard-coded colors.
       Consumers: AiView (manage tab) now, AccountView + onboarding later. -->
	<div class="jv-llm-editor" style="font-family: inherit; color: var(--text)">
		<!-- Blocking progress while a Connect is in flight. The scrim stops the mouse;
	         `inert` on each block below is what stops the KEYBOARD, so Tab cannot walk
	         into the controls underneath. Both are needed - a scrim alone leaves the
	         whole form reachable, and `inert` alone leaves it looking clickable. -->
		<div v-if="busy.active && !hostScrim" class="jv-llm-busy">
			<JvSpinner :size="56" :label="busy.label" />
		</div>

		<div
			v-if="err"
			:inert="busy.active"
			style="color: var(--red); font-size: 13px; margin-bottom: 12px"
		>
			{{ err }}
			<button type="button" class="jv-mon-retry" @click="load">Retry</button>
		</div>

		<!-- ============================================================
         UNIFIED FAILOVER LIST + CONFIG SECTION (!singleMode only - the
         Account/Settings editor). Onboarding never reaches this branch
         (singleMode forces llmMode==='quick' below). Phase 1: read list
         only; config section arrives in phase 2. ============================================================ -->
		<section
			v-if="!singleMode"
			:inert="busy.active"
			style="margin-bottom: 18px; display: flex; flex-direction: column; flex: 1 1 auto"
		>
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
						v-if="canEdit"
						:disabled="!editable"
						@click="directPanelOpen = !directPanelOpen"
						class="jv-btn jv-btn--sm jv-btn--ghost"
					>
						{{ directPanelOpen ? "Close" : "Reconnect" }}
					</button>
					<button
						v-if="canEdit"
						:disabled="!editable"
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

			<!-- Empty/disconnected state. Used to be a left-aligned "No models yet"
	           line with the Add button pushed to the pane's far RIGHT edge (see the
	           end-aligned button below) - and when the pool was empty because of a
	           disconnect, the actual reason ("Your keys and connected accounts were
	           deleted") sat in an unrelated pill down at the pane's bottom, so the
	           two facts read as unrelated instead of one. This box says why (reusing
	           applyStatus's own pill markup verbatim - never a second copy of that
	           sentence) and puts the one way out directly under it. Hidden while the
	           add panel is open: that panel IS the next step, so a second "Add a
	           model" trigger above it would compete with it, not help (mirrors the
	           end-aligned button's own !panel.open guard). -->
			<div v-if="!rows.length && !showDirectRow && !panel.open" class="jv-flist-empty">
				<!-- 'failed' is deliberately excluded here: the savebar below owns that
	             status (it is the one place with a Resync button), so painting it a
	             second time in this box - with no way to retry from it - would be the
	             exact duplication this box exists to remove. Anything else non-idle
	             (today, only 'warn'/disconnected) has no action to offer either way,
	             so it is safe, and more honest, to surface here instead of at the
	             very bottom of the pane. -->
				<span
					v-if="applyStatus.kind !== 'idle' && applyStatus.kind !== 'failed'"
					class="jv-pool-syncpill"
					:class="'jv-pool-syncpill--' + applyStatus.kind"
					role="status"
				>
					<span class="jv-pool-syncpill-ic" aria-hidden="true"></span
					>{{ applyStatus.text }}</span
				>
				<!-- A pool that was simply never set up (fresh tenant, nothing ever
	             applied) has no status to report - "No models yet" is the neutral
	             line for that, not a hardcoded stand-in for the warning above. Also
	             the fallback for 'failed', which the savebar below reports instead. -->
				<p v-else class="jv-flist-empty__msg">No models yet.</p>
				<button
					v-if="canEdit"
					:disabled="!editable"
					@click="openAdd"
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
					Add a model
				</button>
			</div>

			<!-- template wrapper, not v-if on the row div itself: Vue 3 gives v-if
			     higher precedence than v-for on the SAME element, so it would run
			     before `row` exists in scope. See pendingAddUid's doc above for what
			     this v-if is hiding and why. -->
			<template v-for="(row, i) in rows" :key="row._uid ?? i">
				<template v-if="row._uid !== pendingAddUid">
					<!-- Common case (0/1 accounts, or an api-key row): unchanged single row. -->
					<div v-if="!isGroupedRow(row)" class="jv-flist-row">
						<span class="jv-pool-badge">{{ i + 1 }}</span>
						<ProviderLogo
							:provider="row.provider"
							:upstream="row.credentialType === 'subscription' ? row.upstream : ''"
							:size="18"
						/>
						<span class="jv-flist-chip">{{ sourceChip(row) }}</span>
						<span
							class="jv-flist-model"
							:class="{ 'jv-flist-model--unset': !row.model }"
							>{{ rowModelLabel(row) }}</span
						>
						<!-- The connected account's own identity is what tells two subscriptions
						     to the same provider/model apart. isGroupedRow already routed 2+
						     accounts to the grouped rendering below, so this is always exactly
						     the one account on this row - no more "+N more" to fold in. -->
						<span
							v-if="row.credentialType === 'subscription' && row.accounts?.length"
							class="jv-flist-acct"
							:title="accountLabel(row.accounts[0])"
							>{{ accountLabel(row.accounts[0]) }}</span
						>
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
							<!-- Edit is meaningless on a subscription row with no connected account
			         yet: there is nothing to edit. That covers both a freshly added,
			         never-connected row AND one whose only account was disconnected -
			         rows carry no "is this new" flag once the add-panel is closed
			         (a preset-added row and a disconnected row both land here looking
			         identical), so Edit stays keyed on rowHasConnectedAccount for both.

			         Reconnect is DIFFERENT: an accountless row that lost its account to a
			         disconnect needs Reconnect as its own recovery path (jarvis#821
			         review - hiding it forced a destructive Remove + re-add that lost the
			         row's place in the failover order). Since new-vs-disconnected can't be
			         told apart reliably here, Reconnect is offered on every subscription
			         row regardless of rowHasConnectedAccount; showing it on a row still
			         mid-add is harmless (quickReconnect opens the same connect panel
			         "+ Add a model" already would). -->
							<button
								v-if="
									canEdit &&
									(row.credentialType !== 'subscription' ||
										rowHasConnectedAccount(row))
								"
								:disabled="!editable"
								@click="openEdit(i)"
								class="jv-btn jv-btn--sm jv-btn--ghost"
							>
								Edit
							</button>
							<button
								v-if="canEdit && row.credentialType === 'subscription'"
								:disabled="!editable"
								@click="quickReconnect(i)"
								class="jv-btn jv-btn--sm jv-btn--ghost"
							>
								Reconnect
							</button>
							<button
								v-else-if="canEdit && row.credentialType !== 'subscription'"
								:disabled="!editable"
								@click="openEdit(i)"
								class="jv-btn jv-btn--sm jv-btn--ghost"
							>
								Replace key
							</button>
							<!-- One button, two meanings, and the label says which BEFORE it is
				         pressed: with other models left this drops one entry from the
				         failover list; on the last one there is no list left to edit and it
				         tears the whole connection down instead. -->
							<button
								v-if="canEdit"
								:disabled="!editable"
								@click="remove(i)"
								class="jv-btn jv-btn--sm jv-btn--ghost jv-pool-disc"
								:title="
									isLastConnectedRow(row)
										? 'Delete your keys and connected accounts everywhere'
										: 'Remove this model from the failover list'
								"
							>
								{{ isLastConnectedRow(row) ? "Disconnect" : "Remove" }}
							</button>
						</span>
					</div>

					<!-- 2+ accounts on a subscription row: a model row (no account chip, but
					     the SAME Remove the ungrouped row has) followed by one sub-row per
					     account. accountHealth(row) is POOL-WIDE, not per-account (see its
					     own doc), so the health dot/label lives once on the model row; the
					     sub-rows below are identity + order + per-account Disconnect only. -->
					<template v-else>
						<!-- One hoverable wrapper around the model row + all its sub-rows, so
				     hovering anywhere on the card highlights it as one unit (a plain
				     sibling :hover can't reach backwards from a sub-row to the model
				     row above it). -->
						<div class="jv-flist-group">
							<div class="jv-flist-row jv-flist-row--grouped">
								<span class="jv-pool-badge">{{ i + 1 }}</span>
								<ProviderLogo
									:provider="row.provider"
									:upstream="
										row.credentialType === 'subscription' ? row.upstream : ''
									"
									:size="18"
								/>
								<span class="jv-flist-chip">{{ sourceChip(row) }}</span>
								<span
									class="jv-flist-model"
									:class="{ 'jv-flist-model--unset': !row.model }"
									>{{ rowModelLabel(row) }}</span
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
								<!-- Reorder arrows + Edit/Reconnect/Remove, identical to the ungrouped
			         row: this row's position in the failover chain and its whole-model
			         removal are unaffected by its account count. Only the account-level
			         chip/Disconnect drops out here - that lives on the sub-rows below,
			         alongside (not instead of) this row's own Remove. -->
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
									<!-- A grouped row always has 2+ accounts by construction (isGroupedRow),
					         so rowHasConnectedAccount is always true here - kept for the same
					         reason the ungrouped row keys on it, not row identity. -->
									<button
										v-if="canEdit && rowHasConnectedAccount(row)"
										:disabled="!editable"
										@click="openEdit(i)"
										class="jv-btn jv-btn--sm jv-btn--ghost"
									>
										Edit
									</button>
									<button
										v-if="canEdit && rowHasConnectedAccount(row)"
										:disabled="!editable"
										@click="quickReconnect(i)"
										class="jv-btn jv-btn--sm jv-btn--ghost"
									>
										Reconnect
									</button>
									<!-- Whole-model removal, identical to the ungrouped row's Remove: tears
					         down this model and ALL its accounts atomically via remove(i). The
					         sub-row Disconnect below is the other half - it drops one account,
					         not the model - the two are not alternatives, both stay available. -->
									<button
										v-if="canEdit"
										:disabled="!editable"
										@click="remove(i)"
										class="jv-btn jv-btn--sm jv-btn--ghost jv-pool-disc"
										:title="
											isLastConnectedRow(row)
												? 'Delete your keys and connected accounts everywhere'
												: 'Remove this model from the failover list'
										"
									>
										{{ isLastConnectedRow(row) ? "Disconnect" : "Remove" }}
									</button>
								</span>
							</div>
							<div
								v-for="(account, ai) in row.accounts"
								:key="account.account_ref || ai"
								class="jv-flist-subrow"
								:class="{
									'jv-flist-subrow--last': ai === row.accounts.length - 1,
								}"
							>
								<span class="jv-flist-subrow-indent" aria-hidden="true">{{
									ai === row.accounts.length - 1 ? "└─" : "├─"
								}}</span>
								<span class="jv-flist-subrow-avatar" aria-hidden="true">{{
									(accountLabel(account) || "?").charAt(0).toUpperCase()
								}}</span>
								<span
									class="jv-flist-subrow-email"
									:title="accountLabel(account)"
									>{{ accountLabel(account) }}</span
								>
								<span class="jv-flist-subrow-order">{{
									ai === 0 ? "primary" : "backup"
								}}</span>
								<span class="jv-flist-subrow-acts">
									<!-- jarvis#807: promote/demote an account within this row. account[0]
							         is the primary the pool tries first, so Up on the second row makes
							         it primary. Same icon buttons and disabled-at-the-ends rule as the
							         model-row reorder above; the shared order bar commits it. -->
									<button
										v-if="canEdit && row.accounts.length > 1"
										@click="moveAccount(row, ai, -1)"
										:disabled="!editable || ai === 0"
										title="Move up (primary is first)"
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
										v-if="canEdit && row.accounts.length > 1"
										@click="moveAccount(row, ai, 1)"
										:disabled="!editable || ai === row.accounts.length - 1"
										title="Move down"
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
										v-if="canEdit"
										:disabled="!editable"
										@click="removeAccount(row, ai)"
										class="jv-btn jv-btn--sm jv-btn--ghost jv-pool-disc"
									>
										Disconnect
									</button>
								</span>
							</div>
						</div>
					</template>
				</template>
			</template>

			<!-- Ordering is the one change in this editor that does not apply itself
           (see move() for why), so it has to say so and offer a way to commit.
           Shown only while there are unapplied moves. -->
			<div v-if="orderDirty" class="jv-flist-orderbar">
				<span class="jv-flist-orderbar__msg">
					New order not applied. Your agent still uses the previous order.
				</span>
				<button
					:disabled="!editable"
					@click="applyOrder"
					class="jv-btn jv-btn--sm jv-btn--primary"
				>
					Apply order
				</button>
			</div>

			<!-- A real .jv-btn, not a dashed row: this is a LIST ACTION that opens the
           config panel, so it belongs to the same button system as the row actions
           (Edit / Reconnect / Remove). The dashed treatment is reserved for an EMPTY
           SLOT the content will fill -- which is what "+ Connect account" is, inside
           the panel, where the account itself then appears. -->
			<!-- Guarded by (rows.length || showDirectRow): an empty pool already has its
	           OWN centered "Add a model" button in the box above - this end-aligned
	           one is only for a pool that already has something to list. -->
			<button
				v-if="canEdit && !panel.open && (rows.length || showDirectRow)"
				:disabled="!editable"
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
               bench to the provider using what is typed above - never saved, never
               touches the fleet/container (jarvis.llm_key_probe.test_llm_api_key). Motivated
               by a real GLM/Z.ai case: a valid key on a zero-balance account saved cleanly and
               only failed AFTER save with a bare "Not working" - this lets the customer catch
               that (and see the provider's OWN error) before they ever click Save.
               A row with a saved key sends no key at all and asks the server to load its
               own (#679), so a base URL can be changed and tested without re-pasting a
               credential. The result has THREE states, not two: an endpoint this bench
               could not reach is reported neutrally, because chat runs from inside the
               container and reaches addresses the bench cannot (#680). -->
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
						:class="testStatusClass(panel.testResult)"
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
								v-else-if="panel.testResult.verdict === 'unverified'"
								width="14"
								height="14"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2.4"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<circle cx="12" cy="12" r="9" />
								<path d="M12 11v5M12 8h.01" />
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
							><b>{{ testStatusHeadline(panel.testResult) }}</b>
							{{ panel.testResult.message }}</span
						>
					</div>
					<div
						v-if="panel.testResult && panel.testResult.caveat"
						style="font-size: 11px; color: var(--text-3); margin-top: 5px"
					>
						{{ panel.testResult.caveat }}
					</div>

					<!-- Opt-in backups (API KEYS ONLY): when switched on, expands this
               provider into its full single-vendor failover chain on close,
               sharing the same key. Off by default; add-mode only. -->
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
						Add backup models automatically
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

					<!-- Say what this sign-in will actually DO before it starts. The customer
               picked "+ Add a model" for a provider the pool already has, and what
               they will get is another ACCOUNT on the existing model, not a second
               model - because every subscription model shares one Bifrost provider
               entry, so two rows naming one model cannot both exist. Finding that
               out afterwards used to cost a whole wasted OAuth round trip (#575). -->
					<p v-if="addFoldsInto" class="jv-pool-foldnote">
						{{ upstreamLabelOf(panelRow.upstream) }} is already connected. Signing in
						again adds another account to this model, not a new model.
					</p>

					<!-- Connect account: EDIT-mode re-entry only. In add mode the two-step sign-in
               renders directly (see its v-if below), so a fresh "Add a model" never shows
               this button - clicking "Connect account" and THEN "Open sign-in" was a
               redundant double-click for one intent. It reappears only in the EDIT panel
               when a row's last account was disconnected (removeAccount leaves _connect
               closed), giving a neutral re-entry point rather than auto-popping OAuth. -->
					<button
						v-if="
							canEdit &&
							panel.mode !== 'add' &&
							!(panelRow._connect && panelRow._connect.open) &&
							!(panelRow.accounts && panelRow.accounts.length)
						"
						:disabled="!editable"
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
										v-if="canEdit"
										class="jv-btn jv-btn--sm jv-btn--ghost"
										:disabled="!editable"
										@click="openConnectPanel(panelRow, ai)"
										title="Re-authorize to mint fresh tokens"
									>
										Reconnect
									</button>
									<button
										v-if="canEdit"
										class="jv-btn jv-btn--sm jv-btn--ghost jv-pool-disc"
										:disabled="!editable"
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
								v-if="canEdit && !(panelRow._connect && panelRow._connect.open)"
								:disabled="!editable"
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
               URL, since a URL pasted before sign-in has no nonce and finishConnect no-ops.
               The condition lives in panelConnectOpen because the panel's own primary
               action has to know when this spine owns the Connect button. -->
					<div v-if="panelConnectOpen" class="jv-csteps">
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
									:disabled="!editable"
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
                 can't hide them - Connect stays to submit. The footer below owns Cancel
                 for every other flow, but its container can never share a row with this
                 one, so add mode gets its own paired Cancel here instead (Connect on the
                 left, Cancel on the right, as requested) and the footer's copy is
                 suppressed via spineCancelPaired so it does not render a second time. -->
							<div class="jv-cn-acts">
								<button
									v-if="panel.mode !== 'add'"
									:disabled="!editable"
									@click="closeConnect(panelRow)"
									class="jv-btn jv-btn--ghost"
								>
									Cancel
								</button>
								<!-- This IS the save for a chat subscription: the grant it captures
	                     is written to the pool in the same flow, and the button stays
	                     on "Connecting…" until the agent has picked it up. -->
								<button
									@click="finishConnect(panelRow)"
									:disabled="
										!editable ||
										panelRow._connect.loading ||
										!panelRow._connect.authorizeUrl ||
										!(panelRow._connect.pastedUrl || '').trim()
									"
									class="jv-btn jv-btn--primary"
								>
									<JvSpinner
										v-if="panelRow._connect.loading"
										color="currentColor"
									/>
									{{ panelRow._connect.loading ? "Connecting…" : "Connect" }}
								</button>
								<button
									v-if="panel.mode === 'add'"
									type="button"
									:disabled="!editable"
									class="jv-btn jv-btn--ghost"
									@click="closePanel"
								>
									Cancel
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

				<!-- A failed apply is reported HERE, next to the row it belongs to, and the
	               panel is deliberately left open with everything still typed into it so
	               the customer can fix the cause without re-entering a key. -->
				<div v-if="applyResult && applyResult.kind === 'failed'" class="jv-cn-err">
					{{ applyMessage }}
				</div>

				<div class="jv-cfgpanel-acts">
					<!-- Suppressed while spineCancelPaired: the OAuth spine's own Cancel
		                 (jv-cn-acts, add mode) already covers this, paired with its Connect
		                 in the order the product asked for - rendering this one too would
		                 put a second Cancel on screen. -->
					<button
						v-if="!spineCancelPaired"
						type="button"
						:disabled="!editable"
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
					<!-- The panel's ONE primary action. It connects AND applies: there is no
		                 second "Save configuration" step behind it. Absent while the OAuth
		                 spine above is showing, because that spine's own Connect button is
		                 this action for a chat subscription. -->
					<button
						v-if="panelAction"
						type="button"
						:disabled="!editable"
						class="jv-btn jv-btn--primary"
						@click="panelAction.run()"
					>
						{{ busy.active ? "Connecting…" : panelAction.label }}
					</button>
				</div>
			</div>

			<!-- A single model has nothing to fall back to. Name the consequence instead
           of leaving the customer to infer it from "tried in order". Sits at the
           bottom of the panel (not right under the row list) so the top stays
           clean and this settles into the space the panel would otherwise leave
           empty; the section's own flex column (above) plus this hint's
           margin-top: auto is what pins it there. -->
			<div
				v-if="canEdit && !panel.open && rows.length === 1 && !showDirectRow"
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
		</section>

		<!-- ================ QUICK / CUSTOM (shared rows) ================ -->
		<section v-if="singleMode" :inert="busy.active" style="margin-bottom: 18px">
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
					<!-- Onboarding Test (P0-09): the same live, side-effect-free probe the
					     Account panel offers, bound to this single row. "Start chatting"
					     REQUIRES a pass on a freshly-typed remote key (singleModeCanStart);
					     local/private endpoints and stored keys are exempt (they can't be
					     probed from the bench). A failed test keeps every field typed. -->
					<div
						v-if="singleMode"
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
							:disabled="!editable || smTest.testing || !!testBlockedReason(m)"
							:title="testBlockedReason(m) || testButtonHint(m)"
							@click="testSingleModeRow"
						>
							{{ smTest.testing ? "Testing…" : "Test" }}
						</button>
						<span
							v-if="testBlockedReason(m)"
							class="jv-pool-opt"
							style="font-size: 11.5px"
							>{{ testBlockedReason(m) }}</span
						>
						<span
							v-else-if="isLocalProviderRow(m)"
							class="jv-pool-opt"
							style="font-size: 11.5px"
							>Local endpoint - only verifiable from inside your Jarvis
							container</span
						>
					</div>
					<div
						v-if="singleMode && smTest.result"
						class="jv-status"
						:class="testStatusClass(smTest.result)"
						style="margin-top: 10px"
					>
						<span class="jv-status-ic">
							<svg
								v-if="smTest.result.ok"
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
								v-else-if="smTest.result.verdict === 'unverified'"
								width="14"
								height="14"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2.4"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<circle cx="12" cy="12" r="9" />
								<path d="M12 11v5M12 8h.01" />
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
							><b>{{ testStatusHeadline(smTest.result) }}</b>
							{{ smTest.result.message }}</span
						>
					</div>
					<div
						v-if="singleMode && smTest.result && smTest.result.caveat"
						style="font-size: 11px; color: var(--text-3); margin-top: 5px"
					>
						{{ smTest.result.caveat }}
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
									<!-- jarvis#807: promote/demote this account. account[0] is the primary
								         the pool tries first, so Up makes it primary. Same icon buttons and
								         end-disabling as the model-row reorder; shown only when there is
								         more than one account to order. -->
									<button
										v-if="canEdit && m.accounts.length > 1"
										@click="moveAccount(m, ai, -1)"
										:disabled="!editable || ai === 0"
										title="Move up (primary is first)"
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
										v-if="canEdit && m.accounts.length > 1"
										@click="moveAccount(m, ai, 1)"
										:disabled="!editable || ai === m.accounts.length - 1"
										title="Move down"
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
										v-if="canEdit && !singleMode"
										class="jv-btn jv-btn--sm jv-btn--ghost"
										:disabled="!editable"
										@click="startConnect(m, ai)"
										title="Re-authorize to mint fresh tokens"
									>
										Reconnect
									</button>
									<button
										v-if="canEdit"
										class="jv-btn jv-btn--sm jv-btn--ghost jv-pool-disc"
										:disabled="!editable"
										@click="removeAccount(m, ai)"
									>
										Disconnect
									</button>
								</span>
							</div>
							<button
								v-if="canEdit && !singleMode && !(m._connect && m._connect.open)"
								@click="startConnect(m)"
								:disabled="
									m._connect && m._connect.loading && !m._connect.authorizeUrl
								"
								class="jv-pool-addrow"
							>
								+ Add account
							</button>
						</div>
						<!-- Onboarding Test: the same live probe "Start chatting" runs, fired
						     on demand once an account is connected instead of only at the end
						     of the step. See testSubscriptionRow's docstring for why this
						     cannot be a side-effect-free check the way the API-key Test above
						     is. `subtle`/ghost weight only - design.md 3.1 reserves the one
						     `solid` button per surface for "Start chatting". -->
						<div
							v-if="singleMode"
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
								:disabled="
									!editable ||
									hostBusy ||
									subTest.testing ||
									subTest.cooling ||
									!!subTestBlockedReason(m)
								"
								:title="
									subTestBlockedReason(m) ||
									'Sends a real message through your subscription to confirm it works. Costs one message from your plan.'
								"
								@click="testSubscriptionRow(m)"
							>
								{{ subTest.testing ? "Testing…" : "Test" }}
							</button>
						</div>
						<Banner
							v-if="singleMode && subTest.result"
							role="status"
							style="margin-top: 10px"
							:type="subTestBannerType(subTest.result)"
							:message="subTest.result.message"
						/>
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
												:disabled="!editable"
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
												:disabled="!editable"
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
				v-if="isMulti && canEdit"
				:disabled="!editable"
				@click="addModel"
				class="jv-btn jv-btn--sm jv-btn--ghost"
			>
				+ Add model
			</button>
		</section>

		<!-- Status strip. There is no Save button and no blanket "unsaved changes"
	         warning any more: Connect connects AND saves, and so does Remove. Ordering
	         is the one change that waits for the customer, and it says so itself in its
	         own bar next to the list rather than from down here. What is left for this
	         strip is reporting - the outcome of the apply this editor just ran, and
	         otherwise whatever the server last recorded (which covers an apply that is
	         still landing from a previous visit, or one started from another tab).
	         Hidden while busy.active: the scrim's own label already says "Applying to
	         your agent…" next to its spinner, and this strip says the exact same
	         sentence, so showing both is a duplicate, not a second, more useful
	         status (jarvis#559). -->
		<div
			v-if="
				!footerless &&
				statusLine &&
				!busy.active &&
				(!emptyBoxShowing || statusLine.kind === 'failed')
			"
			class="jv-pool-savebar"
		>
			<span
				class="jv-pool-syncpill"
				:class="'jv-pool-syncpill--' + statusLine.kind"
				role="status"
			>
				<span class="jv-pool-syncpill-ic" aria-hidden="true"></span>{{ statusLine.text }}
			</span>
			<!-- jarvis#714: the failed pill had no retry. Hidden while a reorder is
			     still unapplied (orderDirty) - its own "Apply order" bar above already
			     owns committing THAT change, and resync() would otherwise apply it as
			     a side effect with no order-specific confirmation. Hidden while the
			     add/edit panel is open for the same reason "+ Add a model" is
			     (line 249 above): a resync mid-edit would submit whatever is
			     half-typed there instead of leaving it for the customer to finish. -->
			<button
				v-if="canEdit && statusLine.kind === 'failed' && !orderDirty && !panel.open"
				:disabled="!editable"
				@click="resync"
				class="jv-btn jv-btn--sm jv-btn--primary"
			>
				Resync
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
	isContainerOnlyRow,
} from "@/llm/pool";
import { errMessage as _err } from "@/lib/errors";
import { humaniseSyncStatus } from "@/lib/syncStatus";
import { classifyOperation } from "@/lib/llmOperation.js";
import { useConfirm } from "@/composables/useConfirm";
import JvCombo from "@/components/JvCombo.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import DirectSubscriptionCard from "@/components/DirectSubscriptionCard.vue";
import ProviderLogo from "@/components/ProviderLogo.vue";
import Banner from "@/components/Banner.vue";
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
	// Let a host (AiModelsPane) render its OWN busy scrim over the whole settings
	// pane, not just this editor's own box, and suppress the one below so a
	// connect in flight is never blurred twice. `busy` is exposed for the host
	// to read; other consumers (onboarding, ChatView) never pass this, so their
	// scrim is unaffected.
	hostScrim: { type: Boolean, default: false },
	// Onboarding's own apply (saveConnect / "Start chatting") is in flight. Gates
	// the singleMode subscription Test button (below) so the two can never run
	// concurrently - both would push the same desired pool, and letting them race
	// would mean two idempotency keys chasing one config, each blind to the
	// other's operation. `subscriptionTesting` (exposed below) is the mirror: the
	// host reads it to disable its own "Start chatting" while a Test is in flight.
	hostBusy: { type: Boolean, default: false },
});
// "settings-changed" is the footerless (onboarding) passive notice that the desired
// pool was persisted - NOT a control-flow signal (the host controller owns the apply
// transaction). The settings editor keeps using "saved" (runApply) as before.
const emit = defineEmits([
	"saved",
	"ready",
	"direct-changed",
	"settings-changed",
	// The subscription Test above is in flight - the host (OnboardingView) mirrors
	// this into its own "Start chatting" disabled state, same watch+emit idiom as
	// `ready` above, rather than reading the exposed ref reactively through the
	// template ref.
	"subscription-testing",
]);

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
const savedSnapshot = ref("__init__"); // savable pool as of last load/save; drives the dot's staleness
let pollTimer = null;
let pollSettle = null; // resolver for the promise startPolling hands back

// How long a Connect blocks the editor before it hands the wait back to the
// customer. The enqueued job is NOT cancelled at this point and a server-side
// reconciler picks up anything it drops, so timing out here is a UI decision
// ("you should not have to keep staring at this"), never a failure.
const APPLY_TIMEOUT_MS = 90000;
const POLL_MS = 3000;

// The single in-flight apply. Only one can exist: while it runs the whole editor
// is inert, so a second one cannot be started, and no edit can race the payload
// that is already on its way to the fleet.
const busy = ref({ active: false, label: "" });
// Outcome of the last apply THIS editor started: {kind, text, detail}. Distinct
// from `applyStatus` (which reflects whatever the server last recorded, including
// applies started elsewhere) because only this one is worth interrupting for.
const applyResult = ref(null);

// Two flavours of "may the customer touch this", and the split is load-bearing.
//
// canEdit answers "does this customer get edit affordances at all" (props.editable)
// and is what every v-if in the template asks. `editable` shadows the prop with the
// narrower "is the editor accepting input RIGHT NOW", and is what every :disabled
// asks - so one line here disables every control in a 1,500-line template for the
// duration of an apply, including rows the customer was not working on.
//
// They must not be the same binding: gating the v-ifs on busy would UNMOUNT the row
// actions mid-apply, collapsing the list under the spinner overlay that is sitting
// on top of it.
const canEdit = computed(() => props.editable);
const editable = computed(() => props.editable && !busy.value.active);

// The blocking overlay belongs to whichever surface owns the persistence. Onboarding
// (footerless) drives save from its own wizard footer and already covers the same
// wait with its full-screen "Setting up" animation, so a second scrim there would
// only fight it. Pass "" to release.
function setBusy(label) {
	if (props.footerless) return;
	busy.value = label ? { active: true, label } : { active: false, label: "" };
}

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
	if (r.credentialType === "subscription") return rowHasConnectedAccount(r);
	return !!(
		(r.provider || "").trim() &&
		(r.model || "").trim() &&
		((r.apiKey || "").trim() || r.hasKey || isLocalProviderRow(r))
	);
});

// ---- singleMode (onboarding) API-key probe --------------------------------
// The master-detail Test button lives in the !singleMode panel; onboarding's
// singleMode api-key body needs its own, bound to rows[0] (P0-09). Its state is
// separate from `panel` (no panel is ever open in singleMode) but mirrors the same
// stale-guard shape, and `passIdentity` binds a green result to the EXACT fields it
// was earned on so it cannot outlive an edit.
const smTest = ref({
	testing: false,
	result: null, // { ok, message, caveat } | null
	gen: 0,
	passIdentity: "", // fingerprint of the row a PASS is bound to; "" = no live pass
});
// A stable fingerprint of the four fields a probe actually sends. Keyed on the
// provider ID (not its display label) + trimmed model + trimmed key + effective base
// URL, so two field combos cannot collide and a whitespace-only change is a no-op.
function probeIdentityOf(row) {
	if (!row) return "";
	return JSON.stringify([
		providerId(row.provider) || "",
		(row.model || "").trim(),
		(row.apiKey || "").trim(),
		effectiveTestBaseUrl(row),
	]);
}
// Live probe of the singleMode row (same provider probe + stale-guard idiom as
// testApiKeyRow). A PASS binds to the row's current identity; a FAIL leaves every
// entered field untouched (the key is never cleared) so the customer can fix it.
async function testSingleModeRow() {
	const row = rows.value[0];
	if (!row || smTest.value.testing || testBlockedReason(row)) return;
	const myGen = ++smTest.value.gen;
	const boundIdentity = probeIdentityOf(row);
	const stale = () => smTest.value.gen !== myGen;
	smTest.value.testing = true;
	smTest.value.result = null;
	try {
		const res = await api.testLlmApiKey({
			provider: row.provider || "",
			model: row.model || "",
			api_key: row.apiKey || "",
			base_url: effectiveTestBaseUrl(row),
			use_stored_key: usesStoredKey(row) ? 1 : 0,
		});
		if (stale()) return;
		smTest.value.result = testResultOf(res);
		// Only a real pass binds. An "unverified" probe reached nothing, so it must
		// not unlock "Start chatting" the way a pass does (#680) - a typo'd public
		// host fails DNS exactly like a container-only one does.
		smTest.value.passIdentity = res && res.ok ? boundIdentity : "";
	} catch (e) {
		if (stale()) return;
		smTest.value.result = { ok: false, verdict: "fail", message: _err(e), caveat: "" };
		smTest.value.passIdentity = "";
	} finally {
		if (!stale()) smTest.value.testing = false;
	}
}
// Invalidate a stale probe the instant any field it depended on changes (same idiom
// as the panel watch): drop the visible result AND the stored pass, and abandon an
// in-flight probe by bumping the generation.
watch(
	[
		() => rows.value[0]?.provider,
		() => rows.value[0]?.model,
		() => rows.value[0]?.apiKey,
		() => rows.value[0]?.baseUrl,
	],
	() => {
		if (!singleMode.value) return;
		smTest.value.result = null;
		smTest.value.passIdentity = "";
		smTest.value.gen++;
		smTest.value.testing = false;
	}
);
// The onboarding "Start chatting" gate, exposed so the host controller (saveConnect)
// can REQUIRE a passing probe before it opens an apply operation (P0-09). Truthful
// exceptions: a subscription needs a capture/stored account, not a probe; a local /
// container-only endpoint can't be probed from the bench, so provider+model is
// enough; a stored (un-retyped) remote key can't be re-probed either. A remote row
// with a freshly-typed key MUST carry a pass bound to its current fields.
// A visible, definitive rejection on the row as it stands. The two exempt
// branches below (a container-only endpoint, and a stored key) skip the
// pass requirement on the grounds that no useful probe is possible - which
// stopped being true for a stored key once #679 made it testable. Without
// this, a returning customer whose saved key has since been revoked can see
// a red "Test failed. HTTP 401" sitting next to an enabled Start chatting.
// Any edit to the row clears smTest.result, so this only ever reflects the
// fields on screen right now.
function smHardFailure() {
	const res = smTest.value.result;
	return !!(res && !res.ok && res.verdict !== "unverified");
}
const singleModeCanStart = computed(() => {
	if (!singleMode.value) return false;
	const r = rows.value[0];
	if (!r) return false;
	if (r.credentialType === "subscription") return rowHasConnectedAccount(r);
	if (!(r.provider || "").trim() || !(r.model || "").trim()) return false;
	if (smHardFailure()) return false;
	if (isContainerOnlyRow(r)) return true; // local / private endpoint: no bench probe
	const typed = (r.apiKey || "").trim();
	if (!typed) return r.hasKey === true; // stored key: nothing to re-probe
	return smTest.value.passIdentity !== "" && smTest.value.passIdentity === probeIdentityOf(r);
});
// Why "Start chatting" is not available yet, or "" when it is - so the host shows a
// precise inline reason rather than a dead button.
const startBlockedReason = computed(() => {
	if (!singleMode.value) return "";
	const r = rows.value[0];
	if (!r) return "Connect a model to continue.";
	if (r.credentialType === "subscription")
		return rowHasConnectedAccount(r) ? "" : "Connect your account to continue.";
	if (!(r.provider || "").trim()) return "Choose a provider to continue.";
	if (!(r.model || "").trim()) return "Enter a model id to continue.";
	if (smHardFailure()) return "That test failed. Update the settings above to continue.";
	if (isContainerOnlyRow(r)) return "";
	const typed = (r.apiKey || "").trim();
	if (!typed) return r.hasKey ? "" : "Enter an API key to continue.";
	if (smTest.value.passIdentity === probeIdentityOf(r)) return "";
	return "Test your API key before you continue.";
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

// ---- singleMode (onboarding) chat-subscription Test -----------------------
// The API-key Test above (smTest) is a stateless bench-side probe: it never
// touches the fleet or the container. A chat-subscription credential has no
// such side-effect-free check - "does this account work" can only be answered
// by actually routing a real chat completion through the tenant's own running
// pool, which is exactly what a normal apply does. So this Test does not
// invent a second, different check: it fires the SAME save_llm_pool -> admin
// update_llm_pool -> fleet-agent /llm-pool round trip "Start chatting" uses,
// and reads the SAME apply-operation status (chat_readiness_reason and all) -
// then simply does not navigate. A test pass and a real apply can therefore
// never disagree; they are the same probe.
//
// Deliberately its OWN poll loop, separate from the host's single
// createOperationController (OnboardingView's saveConnect owns that one): this
// follows only the operation ITS OWN save_llm_pool call opened, with its own
// idempotency key, so a Test can never dedupe onto - or get superseded by - a
// concurrent Start-chatting attempt. `hostBusy` (passed down from the host) and
// `subscriptionTesting` (exposed up, below) are the two halves of the guard
// that keeps only one of Test / Start-chatting running at a time.
//
// Costs the customer a real chat completion on EVERY click, byte-changing or
// not: it passes force_probe: true (jarvis_admin_v2#297), asking admin to run
// a real probe even against a config identical to the last one, rather than
// let a repeat click reach the fleet-agent's byte-identical no-op path and
// answer from the last verdict on record. So it is never run except on an
// explicit click: no auto-run on mount, no re-run on every keystroke, disabled
// for the request's duration plus a short cooldown after it lands
// (SUB_TEST_COOLDOWN_MS). Each click also mints its OWN fresh idempotency key
// (subTestIdemKey, below): force_probe is not part of admin's idempotency
// fingerprint, so a reused key would resolve through admin's idempotent-reuse
// path and carry the previous verdict forward no matter what force_probe says.
// A host below fleet-agent contract 1.23 does not honour the flag; the polled
// operation's force_probed says whether THIS press actually got a fresh probe
// (see describeTestOutcome), so a customer is never told a carried-forward
// answer is new.
const subTest = ref({
	testing: false,
	cooling: false, // short post-result lockout, see SUB_TEST_COOLDOWN_MS
	result: null, // { kind: "ok"|"fail"|"pending", message } | null
	gen: 0,
});
const SUB_TEST_POLL_MS = 2000;
// Bounded the same as the full editor's own apply wait (APPLY_TIMEOUT_MS,
// above) - a first subscription activation can spend real time bringing up
// the proxy sidecar, and this must not give up sooner than that path does.
const SUB_TEST_TIMEOUT_MS = APPLY_TIMEOUT_MS;
const SUB_TEST_COOLDOWN_MS = 15000;
let subTestCoolTimer = null;
watch(
	() => subTest.value.testing,
	(v) => emit("subscription-testing", v)
);

function subTestIdemKey() {
	try {
		if (typeof crypto !== "undefined" && crypto.randomUUID)
			return `test-${crypto.randomUUID()}`;
	} catch (e) {
		/* fall through to the timestamp+random fallback */
	}
	return `test-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}
// Why the button is disabled, or "" when it isn't. Cooling/testing are read
// directly in the template alongside this (they are timing-based, not a fact
// about the row), so this only ever names a fact about the row itself.
function subTestBlockedReason(m) {
	if (!rowHasConnectedAccount(m)) return "Connect your account before testing.";
	return "";
}
function subTestBannerType(result) {
	if (!result) return "info";
	if (result.kind === "ok") return "success";
	if (result.kind === "fail") return "error";
	return "warning";
}
// Poll ONE apply-operation to a terminal state, entirely separate from the
// host's createOperationController (no shared timers, no shared store, no
// visibility handling - a Test is a short, bounded, one-off wait, not the
// durable resume-across-reload machinery "Start chatting" needs). Returns the
// classifyOperation() descriptor, plus the raw force_probed carried alongside
// it (see describeTestOutcome), on a terminal state, or null on the bounded
// timeout / staleness (a newer Test superseded this one).
async function pollTestOperation(operationId, stale) {
	const deadline = Date.now() + SUB_TEST_TIMEOUT_MS;
	while (Date.now() < deadline) {
		if (stale()) return null;
		let status = null;
		try {
			status = await api.getLlmApplyOperation(operationId);
		} catch (e) {
			// Transient read failure: keep polling until the deadline rather than
			// reporting a fail for a status call that simply hiccuped.
		}
		if (stale()) return null;
		if (status) {
			const ui = classifyOperation(status);
			// force_probed (jarvis_admin_v2#297) rides the raw polled status, not the
			// generic classifyOperation() projection shared with the host's own
			// createOperationController (which never asks for a forced probe and so
			// never reads this). Carried alongside ui rather than folded into the
			// shared shape.
			if (ui.terminal) return { ...ui, forceProbed: status.force_probed };
		}
		await new Promise((r) => setTimeout(r, SUB_TEST_POLL_MS));
	}
	return null;
}
// Turn a terminal (or timed-out) operation into the one line the customer sees.
// `canNavigate` is the EXACT condition "Start chatting" itself uses to decide the
// route is real (state ready AND admin has not flagged chat as blocked) - reused
// here rather than re-deriving "did it work" a second way. A failure quotes
// `chatReadinessReason` verbatim, never reworded: that is the same sentence the
// Connect step's own banner renders elsewhere, so a Test failure and a real one
// never read differently for the identical underlying cause. A stale verdict
// (see `stale` below) still shows that same verbatim sentence, with one extra
// sentence of our own appended after it.
//
// `forceProbed === false` (strict: admin always sends a real boolean here per
// jarvis_admin_v2#297, never null) means THIS press asked for a fresh probe and
// did not get one, most often a host below fleet-agent contract 1.23. The
// verdict below is then whatever admin already had on record, not a new
// answer, and the customer must never be told otherwise - a quota-limited
// customer who just fixed their quota and pressed Test again must not read
// "answered just now" about a check that never re-ran.
function describeTestOutcome(ui, m) {
	const provider = upstreamLabelOf(m && m.upstream);
	if (!ui) {
		return {
			kind: "pending",
			message: "Still checking. This can take a minute, then test again.",
		};
	}
	const stale = ui.forceProbed === false;
	if (ui.canNavigate) {
		return {
			kind: "ok",
			message: stale
				? `This is the result of ${provider}'s last check. A fresh check could not be run this time. Select Start chatting to continue.`
				: `${provider} answered a live check just now. Select Start chatting to continue.`,
		};
	}
	if (ui.phase === "retry" || ui.phase === "rejected") {
		const reason = ui.chatReadinessReason || ui.message || "The check failed. Try again.";
		return {
			kind: "fail",
			message: stale
				? `${reason} This is the previous result. A fresh check could not be run this time.`
				: reason,
		};
	}
	// Ready-but-chat-blocked, superseded, or still finishing at our own bound:
	// nothing definitive yet, and this must never assert a pass or a fail it did
	// not earn.
	return {
		kind: "pending",
		message:
			ui.chatReadinessReason || "Still checking. This can take a minute, then test again.",
	};
}
async function testSubscriptionRow(m) {
	if (!m || subTest.value.testing || subTest.value.cooling || props.hostBusy) return;
	if (subTestBlockedReason(m)) return;
	const myGen = ++subTest.value.gen;
	const stale = () => subTest.value.gen !== myGen;
	subTest.value.testing = true;
	subTest.value.result = null;
	try {
		const payload = buildSavePayload();
		if (payload.error) {
			if (!stale()) subTest.value.result = { kind: "fail", message: payload.error };
			return;
		}
		const res = await api.saveLlmPool(
			payload.models,
			payload.preset,
			"failover",
			subTestIdemKey(),
			true // force_probe: an explicit Test press always asks admin for a fresh probe.
		);
		if (stale()) return;
		if (!res) {
			subTest.value.result = {
				kind: "fail",
				message: "Could not start the check. Try again.",
			};
			return;
		}
		if (res.retry_after_seconds) {
			subTest.value.result = {
				kind: "fail",
				message: "Too many changes in a short time. Wait a moment, then try again.",
			};
			return;
		}
		if (!res.apply_operation) {
			// jarvis#806: a lone chat subscription (one model, one account) is routed to
			// the direct/legacy leg by compute_pool_mode's _lone_direct_capable carve-out
			// (jarvis#715). save_llm_pool answers that path with mode:"legacy" and
			// apply_operation:null: no pool apply operation was minted and no separate
			// probe was run (the creds sync is enqueued async, fire-and-forget). Treating
			// the missing operation as a failure showed a red "Could not start the check"
			// on a perfectly healthy connection. There is genuinely nothing to poll here,
			// so report the honest neutral outcome rather than claiming a check passed that
			// never ran, or a failure that did not happen.
			subTest.value.result = {
				kind: "ok",
				message:
					"Saved. No separate check runs for this connection. Select Start chatting to continue.",
			};
			return;
		}
		const ui = await pollTestOperation(res.apply_operation.operation_id, stale);
		if (stale()) return;
		subTest.value.result = describeTestOutcome(ui, m);
	} catch (e) {
		if (!stale()) subTest.value.result = { kind: "fail", message: _err(e) };
	} finally {
		if (!stale()) {
			subTest.value.testing = false;
			subTest.value.cooling = true;
			clearTimeout(subTestCoolTimer);
			subTestCoolTimer = setTimeout(() => {
				subTest.value.cooling = false;
			}, SUB_TEST_COOLDOWN_MS);
		}
	}
}
// Invalidate a stale/visible result the instant the connected-account set
// changes (a Disconnect, or a fresh OAuth paste-back) - a verdict about the
// PREVIOUS account must never linger under the new one. Does not touch an
// in-flight request (bumping gen alone abandons it, same idiom as smTest above).
watch(
	() => (rows.value[0] && rows.value[0].accounts && rows.value[0].accounts.length) || 0,
	() => {
		if (!singleMode.value) return;
		subTest.value.result = null;
		subTest.value.gen++;
	}
);

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
//
// Every `model` here MUST mirror that provider's api_key is_default in the admin
// seed (jarvis_admin_v2/fleet/provider_catalog.py PROVIDER_SEED, whose generated
// mirror is jarvis/_model_catalog.py). Nothing enforces that, so a catalog refresh
// silently strands this copy: six of these were left behind by the 2026-07-26
// refresh and preselected deprecated ids until 2026-08-06. Re-check the pair when
// bumping either side.
const PROVIDER_DEFAULTS = {
	Anthropic: { model: "claude-sonnet-5", baseUrl: "https://api.anthropic.com" },
	// "gpt-5.6" is this literal's FALLBACK value only, used before the catalog
	// fetch lands or if it fails - providerDefaultModel() below prefers the
	// catalog's is_default flag. Previously a stale "gpt-4o" here (fixed
	// alongside the catalog wiring: PROVIDER_DEFAULTS.OpenAI predates the
	// gpt-5.x rollout and was never updated).
	OpenAI: { model: "gpt-5.6", baseUrl: "https://api.openai.com/v1" },
	// Flash, not pro: Google grants pro-tier models zero free quota, so a
	// gemini-2.5-pro fallback 429s on the free key most customers start with.
	// Matches the catalog's api_key is_default, which this only stands in for.
	"Google Gemini": {
		model: "gemini-3.6-flash",
		baseUrl: "https://generativelanguage.googleapis.com",
	},
	Mistral: { model: "mistral-large-latest", baseUrl: "https://api.mistral.ai/v1" },
	// gpt-oss, not llama: Groq deprecated llama-3.3-70b-versatile on 2026-06-17
	// and points migrations at the gpt-oss routes.
	Groq: { model: "openai/gpt-oss-120b", baseUrl: "https://api.groq.com/openai/v1" },
	"Together AI": {
		model: "meta-llama/Llama-3.3-70B-Instruct-Turbo",
		baseUrl: "https://api.together.xyz/v1",
	},
	// deepseek-chat was deprecated 2026-07-24 and only maps to deepseek-v4-flash
	// during a compatibility window, so it must not be what a new key preselects.
	DeepSeek: { model: "deepseek-v4-flash", baseUrl: "https://api.deepseek.com" },
	"Moonshot (Kimi)": { model: "kimi-k2.6", baseUrl: "https://api.moonshot.ai/v1" },
	"xAI Grok": { model: "grok-4.5", baseUrl: "https://api.x.ai/v1" },
	"GLM / Z.ai": { model: "glm-4.7", baseUrl: "https://api.z.ai/api/paas/v4" },
	// GLM Coding Plan is a separate z.ai subscription from pay-as-you-go "GLM / Z.ai"
	// above - a coding-plan key 402s with "insufficient balance" on the pay-as-you-go
	// base URL even though it's perfectly valid on this one (see apiKeyModelHealth's
	// targeted hint in pool.js for the exact trap this option exists to avoid).
	"GLM / Z.ai (Coding Plan)": {
		model: "glm-4.7",
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
	// A 2+-model custom pool only reads "Proxy (failover)" when badgeMode agrees a
	// sidecar is actually deployed (some enabled model is a subscription). A pure
	// BYO api-key pool of any size is agent-direct - no proxy, so no badge.
	// (was: any 2+ rows counted as "Proxy (failover)" regardless of credential type)
	if (llmMode.value === "custom")
		return validModels.value.length >= 2 && badgeMode.value === "proxy"
			? "Proxy (failover)"
			: "";
	return ""; // quick = single model
});
// Save-bar status pill (Option A - "honest model health"). Reflects the outcome
// of the most recent apply, including any per-account subscription warnings the
// backend surfaced (e.g. a chat subscription that rejected a test request).
// The wording comes from @/lib/syncStatus so this pill, the billing pane's Sync
// row and the skills/agents pills all say the same thing about the same raw value.
// Two kinds are added on top of it and stay local to this editor: "warn" (applied,
// but the fleet flagged individual models) and "idle" (nothing recorded yet, so the
// pill is hidden rather than captioned).
const applyStatus = computed(() => {
	if (sync.value.pending) return { kind: "pending", text: "Applying to your agent…" };
	const st = humaniseSyncStatus(sync.value.last_sync_status);
	// A teardown is the one outcome the server records that is NOT an apply, and it
	// used to fall through to "idle" and hide the strip. That left the whole
	// user-visible result of Disconnect riding on applyResult, a client-only value
	// that retires itself after six seconds and does not survive the pane
	// remounting the editor - so a disconnect that deleted every credential could
	// leave nothing at all on screen (jarvis#574). Reading it off the server makes
	// the outcome durable: it is still true after the toast expires, after a
	// reload, and in a tab that was not the one that pressed the button.
	//
	// Amber, not red, and for the same reason the Connection badge is: nothing is
	// broken, the customer asked for this, and the sentence says how to undo it.
	if (st.kind === "disconnected") {
		return {
			kind: "warn",
			text: "Disconnected. Your keys and connected accounts were deleted. Add a model to use chat again.",
		};
	}
	if (st.kind === "failed") {
		return { kind: "failed", text: st.detail ? `${st.text}. ${st.detail}` : st.text };
	}
	if (st.kind === "ok") {
		let n = Array.isArray(sync.value.warnings) ? sync.value.warnings.length : 0;
		if (n === 0 && sync.value.subscription_status === "unverified") n = 1;
		if (n > 0)
			return {
				kind: "warn",
				text: `Applied · ${n} model${n > 1 ? "s" : ""} need${n > 1 ? "" : "s"} attention`,
			};
		return { kind: "ok", text: "Applied" };
	}
	if (st.kind === "pending") return { kind: "pending", text: "Applying to your agent…" };
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
// Subscription upstream key (openai / google / xai / kimi — what the pool editor
// stores, same keys ProviderLogo maps to a logo) -> its display label. Without the
// full map, Kimi/xAI/Gemini rows all mislabelled as "OpenAI" while showing the
// correct logo. Unknown upstream falls back to the raw value, never a wrong vendor.
const SUB_UPSTREAM_LABELS = {
	openai: "OpenAI",
	google: "Google Gemini",
	xai: "xAI Grok",
	kimi: "Kimi (Moonshot)",
};
function sourceChip(row) {
	if (!row) return "";
	if (row.credentialType === "subscription")
		return "Subscription · " + (SUB_UPSTREAM_LABELS[row.upstream] || row.upstream || "OpenAI");
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
	addBackups: false,
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
		addBackups: false,
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
// The row the customer is in the MIDDLE of adding, or null.
//
// openAdd appends the row up front (so a Connect started inside the panel already
// includes it), but appending is not committing: until the customer presses Connect
// the row is a half-filled form, not a model. An apply started from some OTHER row
// (Remove, Apply order) must therefore leave it completely alone - neither saving it
// half-finished nor letting the load() that follows delete it.
//
// `_committed` is what keeps this honest in the one case where an add-panel row IS
// already on the server: a Connect whose write landed but whose fleet apply then
// failed leaves the panel open on a row save_llm_pool has already stored. Holding
// that row out of the next payload would silently delete a model the customer really
// did connect, so it stops counting as in-progress the moment its write succeeds.
function pendingAddRow() {
	if (!panel.value.open || panel.value.mode !== "add") return null;
	const r = rows.value.find((x) => x._uid === panel.value.uid);
	return r && !r._committed ? r : null;
}
// _uid of the row above, or null. The list below hides that ONE row while it is
// the pool's only entry: rendered as a normal list row it reads as an already-
// connected model ("1 | Subscription · OpenAI | gpt-5.5 | Reconnect / Edit /
// Remove") when nothing has been connected yet -- worst right after a disconnect,
// where "+ Add a model" on an empty pool produced exactly that phantom row. Scoped
// to rows.length === 1 (not every in-progress add) because a SECOND model being
// added to an already-populated pool relies on staying visible in the list, and
// hiding it there would desync the Down-arrow's `i === rows.length - 1` bound and
// move()'s raw-index reorder (both index into the real, unfiltered `rows` array).
const pendingAddUid = computed(() => {
	const r = pendingAddRow();
	return r && rows.value.length === 1 ? r._uid : null;
});
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
		addBackups: false,
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
		addBackups: false,
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
// Whether this probe has to ask the server for the row's SAVED key because the
// customer has not typed one. Only true for a row that really has one stored
// (hasKey), which onProviderChange resets the moment the provider is switched -
// a stored key belongs to the old provider's credential, not this one.
function usesStoredKey(row) {
	return !!(row && !(row.apiKey || "").trim() && row.hasKey === true);
}
// Why the Test button is disabled right now, or "" when it's enabled.
//
// hasKey + a blank apiKey used to be blocked with "Re-enter the key to test it",
// because the probe only ever saw what was in the panel. That made the one edit
// where a test matters most - changing a base URL on a working model - the one
// edit that could not be tested, unless the customer still had a key they pasted
// months ago (#679). The server can resolve that key itself now, so this case is
// allowed and the reason strings below are each true of a DIFFERENT state rather
// than one string covering them all.
function testBlockedReason(row) {
	if (!row) return "Nothing to test";
	if (!(row.provider || "").trim()) return "Choose a provider to test";
	if (!(row.model || "").trim()) return "Enter a model id to test";
	// Local providers (Ollama, vLLM) take no key - nothing blocks the probe.
	if (isLocalProviderRow(row)) return "";
	// A saved key the server can load on our behalf. Nothing to re-enter.
	if (usesStoredKey(row)) return "";
	if (!(row.apiKey || "").trim()) return "Enter an API key to test";
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
	if (usesStoredKey(row)) {
		return (
			"Sends a minimal live request using your saved key and the settings above. " +
			"The key stays on the server and nothing is saved."
		);
	}
	return "Sends a minimal live request to this provider using what's typed above. Nothing is saved.";
}
// The three ways a probe can come back (jarvis.llm_key_probe's `verdict`).
// "unverified" means the bench never reached the endpoint, which is a fact about
// this network and not about the key, so it must not render as a red failure -
// a container-only base URL hits this every time and is perfectly valid (#680).
function testStatusClass(result) {
	if (!result) return "";
	if (result.ok) return "jv-status-ok";
	return result.verdict === "unverified" ? "jv-status-warn" : "jv-status-bad";
}
function testStatusHeadline(result) {
	if (!result) return "";
	if (result.ok) return "Key works.";
	return result.verdict === "unverified" ? "Could not test from here." : "Test failed.";
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
			// The key itself is never sent back to the browser, so an untyped row
			// asks the server to load its own saved one (#679).
			use_stored_key: usesStoredKey(row) ? 1 : 0,
		});
		if (stale()) return;
		myPanel.testResult = testResultOf(res);
	} catch (e) {
		if (stale()) return;
		myPanel.testResult = { ok: false, verdict: "fail", message: _err(e), caveat: "" };
	} finally {
		if (!stale()) myPanel.testing = false;
	}
}
// Flatten a probe response into what the status block renders. `verdict` is what
// decides the colour (testStatusClass); `ok` alone cannot, because a failure and
// an un-run probe are both "not ok" and only one of them is the customer's problem.
function testResultOf(res) {
	const checks = Array.isArray(res && res.checks) ? res.checks : [];
	const last = checks[checks.length - 1];
	return {
		ok: !!(res && res.ok),
		verdict: (res && res.verdict) || (res && res.ok ? "pass" : "fail"),
		message:
			(last && last.detail) ||
			(res && res.ok ? "The provider accepted the request." : "The test failed."),
		caveat: (res && res.caveat) || "",
	};
}

// Opt-in backups (API KEYS ONLY - no subscription presets exist and
// multi-model-per-account is unconfirmed for cliproxy, so subscriptions never
// auto-add backups). Finds the catalog's single-vendor preset for this
// provider, if any. Only consulted when the add panel's backup switch is on.
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

// ---- the panel's ONE primary action --------------------------------------
// Connect is the save. Whatever the credential type, the customer presses one
// button and the model is live on their agent when it releases; there is no second
// step to go and find.

// True while the OAuth sign-in spine is on screen. That spine ends in its own
// Connect button (finishConnect), so the panel must not offer a competing one.
const panelConnectOpen = computed(() => {
	const r = panelRow.value;
	if (!r || r.credentialType !== "subscription") return false;
	const c = r._connect;
	if (!c) return false;
	return !!c.open || (panel.value.mode === "add" && !(r.accounts || []).length);
});

// The already-connected row an ADD-mode subscription sign-in will fold into, or
// null. Drives the notice above the sign-in spine so the customer reads "this adds
// an account to what you already have" BEFORE they authorize, rather than
// discovering it from the fold afterwards. Only ever set in add mode: in the edit
// panel "+ Add account" already says the same thing by where it sits.
const addFoldsInto = computed(() => {
	if (!panel.value.open || panel.value.mode !== "add") return null;
	const r = panelRow.value;
	if (!r || r.credentialType !== "subscription") return null;
	const host = subscriptionHostRow(r);
	return host && (host.accounts || []).length ? host : null;
});

// True exactly when the spine's own Cancel is standing in for the footer's -
// the paste-back add flow above (jv-cn-acts), not the device-code flow (that one
// has no Connect to pair against, so it keeps the footer's Cancel as-is). Read by
// the footer below to skip its own Cancel/Close and avoid showing two.
//
// The explicit panel.source check matters: setPanelSource("preset") leaves
// panelRow.credentialType at whatever it was on the tab the customer came
// from, so switching from Chat subscription to Preset without this guard
// would still read as panelConnectOpen and wrongly hide the preset tab's
// own "Done" - the same trap panelAction below already guards against.
const spineCancelPaired = computed(() => {
	const r = panelRow.value;
	return !!(
		panel.value.source === "subscription" &&
		panelConnectOpen.value &&
		panel.value.mode === "add" &&
		r &&
		r._connect &&
		!r._connect.deviceFlow
	);
});

const panelAction = computed(() => {
	const r = panelRow.value;
	if (!panel.value.open || !r || !canEdit.value) return null;
	// On the preset tab, picking a card IS the action - there is no single row to
	// connect. (The tab ships disabled/"Soon"; this keeps the branch honest.)
	if (panel.value.source === "preset") return null;
	if (panel.value.source === "api_key") {
		// "Connect" while the model is not live yet. Once it is, the same button
		// says "Save and apply": calling it Connect there would imply the model is
		// currently disconnected when the customer is only rotating a key or
		// changing the model id.
		const connected = r.hasKey && panel.value.mode !== "add";
		return {
			label: connected ? "Save and apply" : "Connect",
			run: () => connectApiKeyRow(r),
		};
	}
	if (panelConnectOpen.value) return null;
	// A subscription row with a live account: the only thing left to persist is an
	// edit made inside this panel, e.g. a second account disconnected.
	if (!(r.accounts || []).length) return null;
	return { label: "Save and apply", run: () => runApply() };
});

// API-key Connect: probe the key live BEFORE anything is written, then persist.
// A key the provider rejects must never reach the tenant's container - it would put
// the whole pool through a restart just to arrive broken, and the customer would
// learn about it from a failed chat turn rather than from the button they pressed.
async function connectApiKeyRow(row) {
	if (!row || busy.value.active) return;
	err.value = "";
	setApplyResult(null);
	const blocked = missingApiKeyField(row);
	if (blocked) {
		setApplyResult({ kind: "failed", text: blocked, detail: "" });
		return;
	}
	// Probe only what can actually be sent. A stored key is encrypted server-side and
	// never comes back to the browser, so an untouched row has nothing to probe; and a
	// container-only endpoint is reachable from the CONTAINER rather than from this
	// bench, so a failure here would say nothing about the key while blocking a
	// perfectly good save.
	//
	// Locality is decided by the ADDRESS, not the provider id (jarvis#556). Keying it
	// on ollama/vllm meant any other provider pointed at a private or loopback URL got
	// probed from the bench, could not be reached, and silently refused to connect.
	if ((row.apiKey || "").trim() && !isContainerOnlyRow(row)) {
		// The probe is part of the Connect, so the editor is inert for it too. Released
		// before runApply, which puts the overlay straight back up with its own label -
		// and because nothing awaits in between, the swap costs no repaint.
		setBusy("Checking your key…");
		try {
			await testApiKeyRow(row);
		} finally {
			setBusy("");
		}
		const probe = panel.value.testResult;
		// A probe that produced a definitive REJECTION needs nothing more from us:
		// the red Test result block above the button is already showing the
		// provider's own words for why it refused. A probe that produced NO result
		// at all is different - the request itself failed, the block stays empty,
		// and returning here left the customer pressing Connect against total
		// silence (jarvis#556).
		if (!probe) {
			setApplyResult({
				kind: "failed",
				text: "Could not check this key.",
				detail:
					"The check could not be completed, so the model was not connected. " +
					"Check the base URL and try again.",
			});
			return;
		}
		// "unverified" must NOT block the save. It means the bench could not reach
		// the endpoint, which is the same condition isContainerOnlyRow above skips
		// the probe entirely for - it just was not predictable from the URL. The
		// amber block the customer is looking at says saving is how to apply it, so
		// silently refusing to save here would contradict the screen and leave no
		// way forward at all (#680).
		if (!probe.ok && probe.verdict !== "unverified") return;
	}
	// The "add backup models automatically" switch used to be honoured on Close,
	// which only worked because a Save came afterwards. Expand before the payload is
	// built or the switch would silently do nothing.
	if (panel.value.mode === "add" && panel.value.addBackups) expandApiKeyBackups(row);
	await runApply();
}
// The one field standing between this row and a save, or "" when it is ready.
function missingApiKeyField(row) {
	if (!(row.provider || "").trim()) return "Choose a provider first.";
	if (!(row.model || "").trim()) return "Enter a model id first.";
	if (!(row.apiKey || "").trim() && !row.hasKey && !isLocalProviderRow(row))
		return "Enter an API key first.";
	return "";
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

// A subscription row with 2+ accounts expands into a model row + one sub-row per
// account (the "devhub@aerele.in +1 more" collapse was too terse to tell two
// connected accounts apart). 0/1-account rows keep the plain single-row rendering -
// this must stay false for those so today's common case is pixel-identical.
function isGroupedRow(row) {
	return row.credentialType === "subscription" && (row.accounts?.length || 0) > 1;
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
	// leftover from the previous provider — so picking "GLM / Z.ai" gives glm-4.7,
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
// Reordering stays in memory until the customer explicitly applies it.
//
// Everywhere else in this editor an action applies itself, because "Connect" that
// does not connect is the thing we set out to fix. Reordering is the one deliberate
// exception: applying re-renders the tenant's config and RESTARTS its container, so
// self-applying would mean a casual drag costs a ~30s restart, and walking a model
// from fourth to first would cost three. Debouncing hides that but does not remove
// it, and it leaves the customer unable to say "not yet". So ordering gets one small
// Apply button that appears only once the order is actually dirty.
//
// `orderBaseline` is the order from before the first unapplied move. It doubles as
// the dirty flag (non-null means unapplied moves exist) and as the revert target if
// the write fails.
const orderBaseline = ref(null);
// Onboarding (footerless) never shows this: its wizard footer owns persistence, so
// an order change there is applied by the host along with everything else.
const orderDirty = computed(() => !props.footerless && orderBaseline.value !== null);
function move(i, d) {
	if (busy.value.active) return;
	if (!orderBaseline.value) orderBaseline.value = rows.value;
	rows.value = reorder(rows.value, i, i + d);
}
async function applyOrder() {
	if (busy.value.active || !orderBaseline.value) return;
	const revertRows = orderBaseline.value;
	const { persisted } = await runApply({ revertRows, keepPendingAdd: true });
	// Only clear the baseline on a write that landed. If it failed, runApply has
	// already put the old order back, and the baseline must stay valid for the retry.
	if (persisted) orderBaseline.value = null;
}
// jarvis#807: reorder the accounts WITHIN one subscription row. account[0] is the
// primary the pool falls to first (see get_llm_config / the .jv-flist-subrow-order
// comment), and the backend round-trips the accounts[] array order verbatim
// (save_llm_pool json.dumps'es them in order; _model_accounts json.loads them back),
// so persisting the reordered array is the whole change - no backend edit needed.
//
// Mirrors move() above: onboarding (footerless) keeps it local for the wizard footer's
// single save, and the settings editor defers to the SAME order bar / applyOrder() a
// model-row move uses, so a promote does not restart the container on every click.
// Where move() leans on reorder() copying the OUTER array to keep orderBaseline a valid
// revert target, an account move lives INSIDE a row, so it swaps in a CLONED row (new
// accounts array) and leaves the pre-move row untouched in the baseline for revert.
function moveAccount(m, ai, d) {
	if (busy.value.active) return;
	const accts = (m && m.accounts) || [];
	const to = ai + d;
	if (to < 0 || to >= accts.length) return;
	if (props.footerless) {
		m.accounts = reorder(accts, ai, to);
		return;
	}
	const idx = rows.value.indexOf(m);
	if (idx === -1) return;
	if (!orderBaseline.value) orderBaseline.value = rows.value;
	const next = rows.value.slice();
	next[idx] = { ...m, accounts: reorder(accts, ai, to) };
	rows.value = next;
}

// jarvis#714: the status strip's "Last sync failed" pill had no way to retry.
// This re-runs runApply() over the CURRENT rows.value unchanged - the exact
// re-push applyOrder above already does for an order-only change, and the
// mechanism jarvis_settings.py's _pool_sync_is_redundant docstring names as the
// sanctioned lever: "a prior failed/pending/skipped sync means the container
// may not hold the current pool, so an unchanged re-save is the operator's
// retry lever and must enqueue." No new endpoint - save_llm_pool already
// treats a same-content re-save after a failed sync as retryable, not a no-op.
// If #713 later exposes a dedicated retry endpoint, this call is the one place
// to point at it instead.
async function resync() {
	if (busy.value.active || orderDirty.value || panel.value.open) return;
	await runApply();
}
// The models that actually count as a connection. An open "+ Add a model" panel has
// already put a placeholder in the list and it must not read as a second model - not
// even once the customer has typed a provider and key into it, since nothing about it
// is connected until they press Connect.
function filledRows() {
	const pending = pendingAddRow();
	return rows.value.filter((x) => !isRowEmpty(x) && x !== pending);
}
// True when a subscription row has at least one account the connect flow actually
// placed (capture_id: fresh from a just-finished sign-in; account_ref: a stored one
// loaded from config). THE single predicate for "connected" pool-wide (jarvis#821
// cleanup - this used to be inlined separately at each call site): ready,
// singleModeCanStart, startBlockedReason, subTestBlockedReason, the submit guard in
// buildSavePayload, and the Edit/Reconnect row actions in the template above all
// call this now rather than repeating the (accounts || []).some(...) expression.
// Keyed on this rather than "row was just added" because that is the real UI state:
// a row can sit accountless after a disconnect too, not only right after being
// added.
function rowHasConnectedAccount(row) {
	return !!(row && (row.accounts || []).some((a) => a && (a.capture_id || a.account_ref)));
}
// True when removing this row would empty the pool, which is a disconnect rather than
// an edit. Drives BOTH the row action's label and where remove() routes, so the button
// says what it is about to do before it is pressed. Never in onboarding (footerless):
// there is no connection to tear down there yet, and its wizard footer owns the save.
function isLastConnectedRow(row) {
	if (props.footerless || !row || isRowEmpty(row)) return false;
	const filled = filledRows();
	return filled.length <= 1 && filled[0] === row;
}
async function remove(i) {
	const r = rows.value[i];
	if (!r || busy.value.active) return;
	// An add-in-progress row was never saved (pendingAddRow's doc above explains
	// why it sits in rows.value at all): there is nothing on the server to confirm
	// removing and nothing to persist by dropping it, so this IS closePanel's
	// Cancel, not a Remove. Without this branch it fell into the generic
	// confirm+filter+runApply path below like any committed row, and on a pool
	// with nothing else in it that meant SAVING an empty pool - validatePool
	// (jarvis/llm/pool.js) rejects that with "Add at least one model.", a save-path
	// error surfacing on an action that was never a save (jarvis phantom-row bug:
	// disconnect -> "+ Add a model" -> Remove on the still-unconnected draft).
	const pending = pendingAddRow();
	if (pending && pending._uid === r._uid) {
		rows.value = rows.value.filter((x) => x._uid !== r._uid);
		panel.value = closedPanel();
		return;
	}
	// save_llm_pool rejects an empty pool server-side, and so does the fleet agent.
	// Taking the agent's last model away is therefore a different operation, not a
	// smaller edit: it goes through disconnect_llm, which deletes the credentials
	// everywhere instead of writing a pool nobody will accept.
	if (isLastConnectedRow(r)) {
		await disconnect();
		return;
	}
	// A row with no account/key/has_key was never persisted either -
	// seedRowsFromConfig (jarvis/llm/pool.js) only ever builds a row FROM one of
	// those, so isRowEmpty is proof this row is local-only regardless of how it
	// got here (e.g. closePanel's own source==="preset" exception can leave one
	// behind after Cancel). If nothing else in the pool would survive removing it,
	// dropping it locally already matches what the server has - there is nothing
	// to apply, and nothing to warn the customer they are about to lose.
	const after = rows.value.filter((x) => x._uid !== r._uid);
	if (isRowEmpty(r) && !after.some((row) => !isRowEmpty(row))) {
		rows.value = after;
		return;
	}
	const label = rowModelLabel(r);
	if (
		!(await confirm({
			title: "Remove this model?",
			message: label
				? `"${label}" will be removed from the failover list and your agent updated right away.`
				: "This model will be removed from the failover list and your agent updated right away.",
			confirmLabel: "Remove",
			danger: true,
		}))
	)
		return;
	// Filter by the row's stable handle, not the captured index: confirm() awaits, so
	// an index could go stale if rows.value is re-seeded meanwhile.
	const before = rows.value;
	rows.value = rows.value.filter((x) => x._uid !== r._uid);
	if (props.footerless) return;
	await runApply({ revertRows: before, keepPendingAdd: true });
}
// Tear the whole connection down: every stored key and connected account, on this
// bench AND in the workspace container.
//
// Worded for the outcome, not the mechanism. "Remove your last model" describes an
// edit to a list; what actually happens is that the workspace stops having AI, and
// that is what the customer has to agree to. Remove keeps its own narrower meaning
// (drop one entry from the failover list) and is still what the button says whenever
// another model would be left behind.
//
// Runs through the same busy/inert overlay as an apply: it re-renders the tenant
// config and restarts the container exactly like one, so the editor must not accept
// edits while it is in flight.
async function disconnect() {
	if (busy.value.active) return;
	if (
		!(await confirm({
			title: `Disconnect ${agentName} from AI?`,
			message: `Your API keys and connected accounts will be permanently deleted from ${agentName} and from your workspace container. Chat will stop working until you connect a model. Your chat history, skills and macros are kept.`,
			confirmLabel: "Disconnect",
			danger: true,
		}))
	)
		return;
	err.value = "";
	setApplyResult(null);
	setBusy("Disconnecting…");
	try {
		await api.disconnectLlm();
	} catch (e) {
		// NOT "nothing happened". A disconnect is the one operation in this stack
		// with NO rollback, deliberately: the fleet's llm_disconnect.deprovision
		// destroys the key files BEFORE it restarts the container, and admin commits
		// the blanked credential row BEFORE it calls the host, precisely so that a
		// credential which could not be confirmed destroyed is never reported as
		// kept. disconnect_llm then aborts before clearing THIS bench, so a failure
		// here can genuinely mean the keys are gone from the workspace while the
		// bench still holds its copy and still lists the model.
		//
		// So the honest sentence names both halves and points at the retry. Saying
		// "Could not disconnect." invited the customer to carry on using a workspace
		// whose AI was already torn down. Repeating Disconnect is safe: every leg of
		// it is idempotent.
		setApplyResult({
			kind: "failed",
			text: "Could not confirm the disconnect. Your keys are still stored here, but they may already have been removed from your workspace, so chat may stop working. Try Disconnect again.",
			detail: _err(e),
		});
		return;
	} finally {
		setBusy("");
	}
	// No startPolling: there is no apply to converge on. disconnect_llm calls admin
	// synchronously and only clears the bench once admin has confirmed, so by the time
	// it returns the outcome is already final.
	//
	// This message is the immediate acknowledgement and it retires itself; the DURABLE
	// statement of the same fact is applyStatus reading last_sync_status
	// "disconnected" off the server, so the outcome outlives this toast.
	setApplyResult({
		kind: "ok",
		text: "Disconnected. Your keys and connected accounts have been deleted.",
		detail: "",
	});
	// Assert the disconnected state LOCALLY instead of trusting the reseed below to
	// observe it. load() rebuilds rows from a fresh getLlmConfig(); when that call
	// throws, its catch sets err and leaves rows UNTOUCHED, so the model that was just
	// deleted stayed on screen underneath a "Disconnected" banner - and rendered as
	// "Pending re-check", because a snapshot that was never reconciled reads as dirty.
	// The server half is already final by this line (see the note above), so stating it
	// here is not optimism, and it makes the stale row impossible on every path rather
	// than only on the one where the refetch happens to succeed.
	//
	// Success path only: the catch above returns early, so a disconnect that failed
	// still leaves the row exactly where it was for the customer to retry.
	cfg.value = { ...(cfg.value || {}), models: [], preset: "", proxy_active: false };
	rows.value = [];
	selectedPreset.value = "";
	keysByVendor.value = {};
	// An open Edit panel is pointing at a row that no longer exists.
	panel.value = { ...panel.value, open: false, uid: null, testResult: null };
	// An empty pool is the SAVED state now, not unsaved work. Without this the editor
	// reads dirty, which is what flips every health pill to "Pending re-check".
	savedSnapshot.value = poolSnapshot([]);
	await load();
	// Same signal an apply emits, so the host pane re-reads its own state (the DIRECT
	// subscription probe in particular, which a disconnect also clears).
	emit("saved", sync.value);
}

// Disconnecting an account persists itself, like every other mutating action here.
//
// It used to only filter the local array: the chip vanished, nothing was written, and
// the next load() (any apply, any reopen of the pane) brought the account back still
// connected. The customer had been told they disconnected something that was in fact
// still live on their agent, which is the one kind of wrong this editor must not be.
//
// Onboarding (footerless) stays local on purpose: its wizard footer owns the single
// save, there is no agent to disconnect from yet, and applying here would fight it.
async function removeAccount(m, idx) {
	if (!m || busy.value.active) return;
	const prev = m.accounts || [];
	if (idx < 0 || idx >= prev.length) return;
	if (props.footerless) {
		// Onboarding keeps this local (the wizard footer owns the save). If the account
		// being removed is a just-connected one whose capture is still held server-side,
		// revoke + erase it so an abandoned sign-in leaves no live capture behind
		// (plan-05 D2). Best-effort: a failed cancel must never block the UI removal.
		const gone = prev[idx];
		if (gone && gone.capture_id) {
			api.cancelPendingOauthCapture(gone.capture_id).catch(() => {});
		}
		m.accounts = prev.filter((_, j) => j !== idx);
		return;
	}
	const last = prev.length === 1;
	// An accountless subscription row cannot answer a turn, so prunedForSave drops it
	// and the model leaves the failover list along with its last account. That would
	// empty the pool, which this path cannot express - save_llm_pool refuses an empty
	// list. Point at the two things that CAN be done instead, one of which is now the
	// row's own Disconnect. Still far clearer than validatePool's "Model <id> needs at
	// least one connected account" on a model id this editor never showed the customer.
	if (last && filledRows().length <= 1) {
		setApplyResult({
			kind: "failed",
			text: "This is the only account on your only model. Use Disconnect on the model to delete it everywhere, or add another model first.",
			detail: "",
		});
		return;
	}
	const who = accountLabel(prev[idx]);
	if (
		!(await confirm({
			title: "Disconnect this account?",
			message: last
				? `${who} will be disconnected. "${rowModelLabel(
						m
				  )}" has no other account, so it leaves your failover list too, and your agent is updated right away.`
				: `${who} will be disconnected and your agent updated right away.`,
			confirmLabel: "Disconnect",
			danger: true,
		}))
	)
		return;
	// Re-resolve the row after the await: confirm() yields, and an apply finishing in
	// that window reseeds rows.value, leaving `m` detached from the list. Mutating a
	// detached object would drop the disconnect silently.
	const live = rows.value.find((x) => x._uid === m._uid);
	if (!live) return;
	const now = live.accounts || [];
	if (idx >= now.length) return;
	live.accounts = now.filter((_, j) => j !== idx);
	const { persisted } = await runApply();
	// runApply's revertRows only restores the rows ARRAY, which is untouched here, so
	// the chip is put back by hand when nothing was written.
	if (!persisted) live.accounts = now;
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
	const email = (a && a.account_email) || "";
	if (email) return email;
	// Accounts connected before the backend email fix carry neither a label nor an
	// email, so two of them used to both fall through to the same generic string and
	// stay visually identical. Fall back to a short per-account identifier from the
	// account_ref tail (same convention as Kimi's server-side "Kimi <tail>" label,
	// lowercase). Six chars, not four, so two distinct refs are very unlikely to
	// collide back into one label; never the full SUB_<hex> token.
	const ref = (a && a.account_ref) || "";
	if (ref) return "Account " + ref.slice(-6);
	return "Account connected";
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

// Connect-flow error codes -> customer-facing copy. jarvis/oauth/api.py's _err(code,
// message) sends the RAW developer message ("nonce not recognized", "nonce has
// expired; generate a new sign-in URL", ...) as `message`; showing that verbatim to a
// customer is meaningless jargon. This maps the small, closed set of codes the
// connect/complete/device-poll paths can return to something a non-technical customer
// can actually act on. A code that isn't in this map (unknown_provider,
// device_flow_required - both rare, and already readable) deliberately falls through
// to the backend's own message in connectErrorMessage() below, so nothing is ever
// hidden or silently dropped.
const CONNECT_ERROR_COPY = {
	unknown_nonce:
		'Your sign-in session was lost. Click "Open sign-in" and finish the steps in one go without pausing.',
	expired: 'Your sign-in link expired. Click "Open sign-in" to get a fresh one.',
	state_mismatch:
		'That sign-in didn\'t match this attempt. Click "Open sign-in" and paste the new link.',
	missing_code:
		"That doesn't look like the callback URL. After approving, copy the FULL URL from the address bar and paste it here.",
	not_pending: 'This sign-in was already used. Click "Open sign-in" to start a new one.',
	token_exchange_failed:
		'The provider rejected the sign-in. Click "Open sign-in" and try again.',
	code_invalid: 'The provider rejected the sign-in. Click "Open sign-in" and try again.',
	auth_failed: 'The provider rejected the sign-in. Click "Open sign-in" and try again.',
	network_error:
		"Couldn't reach the sign-in provider. Check your connection and try again in a minute.",
	device_start_failed: "Couldn't start sign-in with the provider. Try again.",
};
// Pull a customer-facing message out of the {ok:false, error:{code, message}} envelope
// startConnect/finishConnect/_pollDeviceConnect all get back on failure. A known code
// wins; an unmapped code (or a response with no code at all, e.g. a plain network
// throw) falls back to the backend's own `message`, and only then to the caller's
// generic `fallback` - so this can never crash and never hides a real error.
function connectErrorMessage(res, fallback) {
	const err = res && res.error;
	if (!err) return fallback;
	return CONNECT_ERROR_COPY[err.code] || err.message || fallback;
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
			m._connect.error = connectErrorMessage(res, "Couldn't start sign-in. Try again.");
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
			m._connect.error = connectErrorMessage(res, "Sign-in failed. Start again.");
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
// The row a connect on `r` really belongs to: an EXISTING subscription row that
// already names the same model, or null when `r` is the only one.
//
// "+ Add a model" seeds its row on the chosen provider's DEFAULT model id, so a
// customer connecting a SECOND account of a provider they already use lands on a
// row naming a model the pool already has. Every subscription model renders
// through ONE shared Bifrost provider entry ("cliproxy-subs"), so that pair
// renders duplicate routing targets and llm_proxy.validate() refuses the WHOLE
// spec with `duplicate_subscription_model` - after the sign-in, which is the worst
// possible moment to find out (#575). Several accounts of one provider is the
// point of the subscription tier, so the account belongs on the row that exists.
//
// Keyed on the model id, which is what the shared validator keys its own check on;
// the upstream has to agree too, so a fold can never mix two providers' OAuth
// accounts onto one row.
function subscriptionHostRow(r) {
	if (!r || r.credentialType !== "subscription") return null;
	const model = (r.model || "").trim();
	if (!model) return null;
	return (
		rows.value.find(
			(x) =>
				x !== r &&
				x.credentialType === "subscription" &&
				(x.model || "").trim() === model &&
				(x.upstream || "openai") === (r.upstream || "openai")
		) || null
	);
}

// Shared account placement for both the paste-back (finishConnect) and
// device-code (_pollDeviceConnect) success paths.
async function _placeConnectedAccount(row, d) {
	// Fold onto the row that already owns this model, dropping the duplicate the
	// add-flow seeded. The server folds too (onboarding._coalesce_subscription_models,
	// which covers desk/API clients), but doing it here keeps the failover list and
	// the payload honest instead of shipping a pair we know will be refused.
	const host = subscriptionHostRow(row);
	const m = host || row;
	if (host) {
		if (!host._connect) host._connect = blankConnect();
		host._connect = { ...host._connect, ...row._connect, reconnectIdx: null };
		rows.value = rows.value.filter((x) => x !== row);
		// Keep the open panel on a row that still exists, or its watcher would close
		// it out from under the apply that is about to start.
		if (panel.value.open && panel.value.uid === row._uid) {
			panel.value = { ...panel.value, mode: "edit", uid: host._uid };
		}
	}
	if (!Array.isArray(m.accounts)) m.accounts = [];
	const acct = {
		upstream: m.upstream || "openai",
		account_ref: d.account_ref,
		label: d.label || d.account_email || d.account_ref,
		account_email: d.account_email || "",
		// The OAuth blob never crosses the wire (plan-05 D2): the server holds it
		// under this capture id and merges it by account_ref at save time. A stored
		// (reloaded) account carries neither - the backend keeps its blob.
		capture_id: d.capture_id || "",
		// Provider-stable subject (if the provider gave one) — a durable fold key
		// that survives a reload, unlike the email/label which get_llm_config drops.
		provider_subject: d.provider_subject || "",
		connected: true,
	};
	const ri = m._connect.reconnectIdx;
	// Device-code accounts (Kimi) carry NO email, so this can't fold two captures of
	// the same account. To REFRESH an existing device account use its per-slot
	// Reconnect (sets reconnectIdx, replacing that slot); a generic Connect always
	// appends (intended for pooling a genuinely different account). Their labels are
	// per-account ("Kimi <last 4 of ref>"), so the label fallback below does not fold
	// them either.
	//
	// A STORED account carries no `account_email` at all: get_llm_config returns only
	// {upstream, account_ref, label}, and that label IS the email the sign-in
	// reported. Matching on account_email alone therefore went blind the moment the
	// page reloaded, so signing in again as the SAME ChatGPT user appended a second
	// copy of one account - two cliproxy credential files holding one refresh token,
	// which is the reuse pattern OpenAI revokes whole accounts over. Fall back to the
	// label so a reloaded account still folds onto itself.
	// Prefer the provider-stable subject when present (P1-07): it is the same across
	// every sign-in of one account and survives a reload. Fall back to email/label
	// for providers/accounts that carry no subject (e.g. device-code upstreams, or a
	// stored account get_llm_config returned with only {upstream, account_ref, label}).
	const identityOf = (a) =>
		((a && (a.provider_subject || a.account_email || a.label)) || "").trim().toLowerCase();
	const identity = identityOf(acct);
	const byEmail = identity ? m.accounts.findIndex((a) => identityOf(a) === identity) : -1;
	if (ri != null && ri >= 0 && ri < m.accounts.length) {
		m.accounts.splice(ri, 1, acct);
		if (byEmail >= 0 && byEmail !== ri) m.accounts.splice(byEmail, 1);
	} else if (byEmail >= 0) m.accounts.splice(byEmail, 1, acct);
	else m.accounts.push(acct);
	// Connect IS the save. The grant only lives in this component until the pool is
	// written, so there is no honest state in which the customer has "connected" but
	// not saved - which is why the pane-level Save button is gone. Onboarding
	// (footerless) is the exception: its wizard footer owns the save, and its own
	// completion screen owns the wait.
	if (props.footerless) {
		m._connect = blankConnect();
		return;
	}
	// Leave the sign-in spine standing, with its Connect button still spinning, until
	// the apply resolves. Blanking _connect here would tear the spine down the instant
	// the grant landed, so the button the customer pressed would vanish mid-wait and
	// the panel would reflow underneath the overlay.
	m._connect.loading = true;
	await runApply();
	m._connect = blankConnect();
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
	// The token exchange is part of the Connect, so the editor is inert for it. It is
	// released before _placeConnectedAccount, which owns the apply and raises the
	// overlay again with its own label.
	setBusy("Connecting your account…");
	let res = null;
	try {
		res = await api.completePoolAccountSignin(m._connect.nonce, m._connect.pastedUrl.trim());
	} catch (e) {
		m._connect.loading = false;
		m._connect.error = _err(e);
		return;
	} finally {
		setBusy("");
	}
	// Same {ok, data} envelope as begin - unwrap + surface errors.
	if (!res || res.ok === false) {
		m._connect.loading = false;
		m._connect.error = connectErrorMessage(
			res,
			isCodeOnlyPaste(m.upstream)
				? "Couldn't connect the account. Check the pasted code and try again."
				: "Couldn't connect the account. Check the pasted URL and try again."
		);
		return;
	}
	// Place the (re)connected account. The backend mints a fresh account_ref on
	// every sign-in, so it can't be a dedupe key: a per-account Reconnect refreshes
	// that exact slot (reconnectIdx); otherwise fold onto an existing account with
	// the same email; otherwise append a new one. The just-minted OAuth blob lives
	// only in memory until the pool is saved, so _placeConnectedAccount persists
	// immediately (unless footerless onboarding, where the host CTA drives save).
	await _placeConnectedAccount(m, res.data || {});
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
// accounts carry no capture_id (a stored account's blob lives server-side, keyed
// by account_ref) - reconnect to change; they render as "connected" via their label.
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
			accounts: (r.accounts || []).map((a) => ({ ...a, capture_id: "" })),
			_connect: blankConnect(),
		};
	});
}

// Re-attach the current user's still-active OAuth captures (server-held, un-
// consumed, un-expired) so a reload after a sign-in - or the error-banner Retry,
// which re-runs load() - shows the account connected without a second sign-in
// (plan-05 D2 / P0-04, P1-05). A capture whose account_ref already sits on a row
// just backfills that row's capture_id; one with no matching row (a fresh reload
// where the row was never saved) is attached to the singleMode row so onboarding
// resumes. Only the capture_id + a safe label are stored - never a blob. Fully
// best-effort: any error leaves the rows exactly as they loaded.
async function rehydratePendingCaptures() {
	let resp;
	try {
		resp = await api.getPendingOauthCaptures();
	} catch (e) {
		return;
	}
	const captures = (resp && resp.data && resp.data.captures) || [];
	for (const c of captures) {
		if (!c || !c.capture_id || !c.account_ref) continue;
		const row = rows.value.find(
			(r) =>
				r.credentialType === "subscription" &&
				(r.accounts || []).some((a) => a && a.account_ref === c.account_ref)
		);
		if (row) {
			const a = row.accounts.find((x) => x && x.account_ref === c.account_ref);
			if (a && !a.capture_id) a.capture_id = c.capture_id;
			continue;
		}
		// No row holds it: attach to the singleMode (onboarding) row so a reload
		// mid-onboarding resumes rather than showing an empty connect form.
		if (singleMode.value && rows.value[0]) {
			const r0 = rows.value[0];
			if (r0.credentialType !== "subscription") setCredType(r0, "subscription");
			if (!Array.isArray(r0.accounts)) r0.accounts = [];
			if (!r0.accounts.some((a) => a && a.account_ref === c.account_ref)) {
				r0.accounts.push({
					upstream: c.upstream || r0.upstream || "openai",
					account_ref: c.account_ref,
					label: c.label || c.account_email || c.account_ref,
					account_email: c.account_email || "",
					capture_id: c.capture_id,
					provider_subject: c.provider_subject || "",
					connected: true,
				});
			}
		}
	}
}

// opts.carry: a row to keep across the reseed (the in-progress add row - see
// pendingAddRow). It is not on the server, so seeding from get_llm_config would
// simply delete it, the panelRow watcher would close the panel on top of it, and
// the customer's half-started "+ Add a model" would be gone with no message at all.
// Re-appending it here, inside the SAME assignment that reseeds, keeps the panel
// pointing at the same row object with everything typed into it intact.
//
// (Called from the template's Retry button too, which passes a click Event - hence
// the defensive read rather than destructuring.)
async function load(opts = {}) {
	const carry = (opts && opts.carry) || null;
	err.value = "";
	try {
		cfg.value = (await api.getLlmConfig()) || cfg.value;
		rows.value = carry ? [...seedRows(cfg.value), carry] : seedRows(cfg.value);
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
			// Deliberately no blank placeholder row here. For an EMPTY pool - no preset,
			// no rows, which is exactly what a disconnect leaves - this branch is the one
			// that runs, so this was the line that put a placeholder back no matter which
			// editor came after it. Onboarding still gets its row from the singleMode
			// branch immediately below, which is the only place that actually needs one
			// (its whole UI is rows[0]); the settings list wants its real empty state.
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
			// No blank placeholder row here, deliberately. Onboarding (above) needs one
			// because its whole UI IS rows[0], but this list renders an explicit empty
			// state ("No models yet. Add one below.") plus an always-present "Add a
			// model" button. Seeding a blank row instead meant an empty pool - the state
			// a disconnect leaves behind - showed a half-rendered row numbered "1" with
			// no provider and no model, which reads as "something is still connected".
		}
		// Baseline for the unsaved-changes notice - the pool as just loaded is clean.
		// The carried row is deliberately NOT in the baseline: it is unsaved work, and
		// it should read as unsaved here exactly as it does the moment "+ Add a model"
		// appends it.
		savedSnapshot.value = poolSnapshot(
			carry ? rows.value.filter((r) => r !== carry) : rows.value
		);
	} catch (e) {
		err.value = _err(e);
	}
	// Re-attach any still-active server-held OAuth capture (plan-05 D2 / P0-04,
	// P1-05): a reload after a sign-in - and the error-banner Retry, which re-runs
	// load() - then resumes the SAME capture without a second sign-in. Runs after the
	// clean baseline above so a rehydrated capture reads as the unsaved work it is.
	await rehydratePendingCaptures();
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
	// Onboarding (singleMode) never renders the preset picker (modes=['quick']
	// hides the "From a preset" tab, which is disabled/"Soon" anyway) and
	// nothing in the singleMode branch reads `catalog`, so skip the fetch
	// there. The Account/Settings editor (!singleMode) still loads it for
	// resilient-by-default backups (expandApiKeyBackups) and the preset tab.
	if (!singleMode.value) {
		try {
			catalog.value = (await api.getPresetCatalog()) || [];
		} catch (e) {
			/* backend bundled fallback */
		}
	}
	try {
		modelCatalog.value = (await api.getModelCatalogUi()) || modelCatalog.value;
	} catch (e) {
		/* built-in literal fallbacks below cover this */
	}
}

// Stable string of the savable pool + preset - the cheap key the dirty-notice
// and snapshot reset compare against.
function poolSnapshot(src = rows.value) {
	try {
		return JSON.stringify({ m: buildSaveModels(src), p: selectedPreset.value });
	} catch (e) {
		return "";
	}
}

// Build the per-row backend shape save_llm_pool expects (matches AiView + desk).
function buildSaveModels(sourceRows) {
	return (sourceRows || []).map((r, i) => {
		if (r.credentialType === "subscription") {
			return {
				// jarvis#756: the Provider select for a subscription row writes to
				// r.upstream (onUpstreamChange), never to r.provider - so this row's
				// OWN `provider` field is never touched by the UI. Without this line
				// the posted row carried no `provider` key at all, save_llm_pool's
				// normalize_provider(m.get("provider")) then defaulted to "", and the
				// STORED row saved a subscription with an empty provider even though
				// the customer had picked one. Admin later rejected the apply with
				// "provider + model required in oauth mode", but by then the wizard
				// had already told the customer their connection was saved.
				// UPSTREAM_OAUTH_PROVIDER is the same upstream-value -> label map the
				// OAuth sign-in call already uses (line ~3455), so a row that never
				// resolves a label (should not happen - setCredType defaults upstream
				// to "openai") posts an EMPTY provider here rather than a silently
				// invented one - validatePool (jarvis/llm/pool.js) then refuses the
				// save locally with a clear message instead of letting admin be the
				// only thing that ever checks this.
				provider: UPSTREAM_OAUTH_PROVIDER[r.upstream] || "",
				model: (r.model || "").trim(),
				order: i,
				subscription: {
					rotation: r.rotation || "sticky",
					accounts: (r.accounts || []).map((a) => ({
						upstream: a.upstream || "openai",
						account_ref: a.account_ref,
						label: a.label,
						// A freshly-captured account sends its capture_id; the server merges
						// the held blob by it. A STORED account (reloaded from get_llm_config)
						// sends neither - correct: the backend keeps its blob by account_ref.
						// The OAuth blob itself is never sent from the browser (plan-05 D2).
						capture_id: a.capture_id || "",
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
// An accountless SUBSCRIPTION row used to be exempt from this, deliberately, so that
// validation would say "connect your account" rather than let an in-progress row
// silently vanish. That guard belonged to the pane-level Save button, which could be
// pressed at any moment with a half-filled row on screen. A save can now only start
// FROM a row (Connect) or from an unrelated one (Remove, reorder) - and "+ Add a
// model" seeds an empty subscription row the moment the panel opens, so the exemption
// meant that opening the panel and then removing some OTHER model failed validation
// on a row the customer had not finished starting. Onboarding keeps its own, clearer
// "Connect your account to continue." pre-check in buildSavePayload, so nothing is
// lost by pruning here.
//
// `exclude` is the in-progress add row (see pendingAddRow), held out entirely rather
// than pruned-if-empty: a half-typed API-KEY row is not empty, so pruning alone would
// either save it unfinished or fail an unrelated Remove on validation the customer
// cannot connect to anything they did.
//
// If pruning would empty the pool we keep every row, so validation still speaks up
// instead of quietly saving nothing.
function prunedForSave(src, exclude = null) {
	const all = (src || []).filter((r) => r !== exclude);
	const kept = all.filter((r) => !isRowEmpty(r));
	return kept.length ? kept : all;
}

// Everything save_llm_pool needs, or {error} with the one sentence to show. Split
// out of save() so the settings editor's Connect can validate + build the SAME
// payload without inheriting save()'s "return the moment the row is written".
function buildSavePayload({ exclude = null } = {}) {
	let saveModels, savePreset;
	if (llmMode.value === "preset") {
		const e = selectedEntry.value;
		if (!e) return { error: "Pick a preset." };
		// This used to be enforced by disabling the Save button. With no Save button
		// it has to be a real check, or a preset would apply with blank keys.
		if (saveBlocked.value)
			return {
				error: `Provide keys for: ${missingVendors.value.map(providerLabel).join(", ")}`,
			};
		saveModels = presetToModels(e, keysByVendor.value);
		savePreset = selectedPreset.value || null;
	} else {
		// Quick saves a single-model pool (rows[0]); Custom saves the full pool.
		// Exception: onboarding's singleMode keeps seeded tail rows (editorRows only
		// renders the first) so a returning customer's existing pool isn't dropped.
		const src = prunedForSave(rows.value, exclude);
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
		if (r0 && r0.credentialType === "subscription" && !rowHasConnectedAccount(r0)) {
			return { error: "Connect your account to continue." };
		}
	}
	const v = validatePool(saveModels, savePreset);
	if (!v.ok) return { error: v.error };
	return { models: saveModels, preset: savePreset };
}

// Persist and return. The apply itself finishes in a durable background operation,
// so this says nothing about whether the agent picked the config up.
//
// This is the ONBOARDING entry point (footerless: the wizard's own footer button
// calls it through defineExpose). In footerless mode the editor is NOT the observer:
// it just persists the desired pool and hands back the durable apply-operation
// descriptor for the host's single controller (saveConnect) to follow (plan-05 D2,
// P0-02/P1-01/P1-02). It deliberately does NOT startPolling, does NOT run load()
// (which would reseed the row out from under a running apply), and does NOT treat
// its "saved" emit as control flow - it emits a PASSIVE "settings-changed" for any
// other host that wants to re-read, and returns { ok, result } instead of throwing.
//
// idempotencyKey (footerless): the host owns it and persists it for resume, so a
// re-call after a lost response dedupes on admin rather than minting a second
// desired version. The settings editor uses runApply below instead, unchanged.
async function save(idempotencyKey = "") {
	err.value = "";
	const payload = buildSavePayload();
	if (payload.error) {
		err.value = payload.error;
		return props.footerless ? { ok: false, error: payload.error } : false;
	}
	saving.value = true;
	if (props.footerless) {
		try {
			const result = await api.saveLlmPool(
				payload.models,
				payload.preset,
				"failover",
				idempotencyKey || ""
			);
			// Passive only: NOT the transaction's control flow (the host controller owns
			// what happens next). A host that only wants to re-read state may listen.
			emit("settings-changed");
			return { ok: true, result };
		} catch (e) {
			err.value = _err(e);
			return { ok: false, error: _err(e) };
		} finally {
			saving.value = false;
		}
	}
	try {
		await api.saveLlmPool(payload.models, payload.preset, "failover");
		try {
			sync.value = await api.getLlmSyncStatus();
		} catch (e) {
			/* keep prior */
		}
		startPolling();
		emit("saved", sync.value);
		await load();
		return true;
	} catch (e) {
		err.value = _err(e);
		return false;
	} finally {
		saving.value = false;
	}
}

// Persist AND stay on it until the tenant has actually picked the config up.
//
// The customer just pressed a button that claims a model is connected, so
// returning at save_llm_pool - which only writes the child table and enqueues the
// push to the fleet - would be claiming an outcome we have not seen. The editor
// goes inert for the duration (see `busy`), then reports what really happened.
//
// Returns {persisted, outcome}. persisted:false means NOTHING was written, so an
// optimistic list mutation (reorder, remove) can be put back.
//
// keepPendingAdd marks an apply the customer started from a DIFFERENT row. Such an
// apply must not touch the row an add-panel is still open on (see pendingAddRow):
// it is held out of the payload here and carried across the reseed below.
async function runApply({ revertRows = null, keepPendingAdd = false } = {}) {
	if (busy.value.active) return { persisted: false, outcome: null };
	err.value = "";
	setApplyResult(null);
	const pending = keepPendingAdd ? pendingAddRow() : null;
	const payload = buildSavePayload({ exclude: pending });
	if (payload.error) {
		if (revertRows) rows.value = revertRows;
		setApplyResult({ kind: "failed", text: payload.error, detail: "" });
		return { persisted: false, outcome: null };
	}
	setBusy("Applying to your agent…");
	try {
		await api.saveLlmPool(payload.models, payload.preset, "failover");
	} catch (e) {
		if (revertRows) rows.value = revertRows;
		setApplyResult({ kind: "failed", text: "Could not save your models.", detail: _err(e) });
		setBusy("");
		return { persisted: false, outcome: null };
	}
	// The write landed WITH the add-panel's row in it, so that row is no longer
	// in-progress: the server has it, and a later unrelated apply must keep sending it.
	// Only when it really went into the payload - prunedForSave still drops an empty one.
	if (!pending) {
		const justSaved = pendingAddRow();
		if (justSaved && !isRowEmpty(justSaved)) justSaved._committed = true;
	}
	let outcome;
	try {
		outcome = await startPolling({ timeoutMs: APPLY_TIMEOUT_MS });
	} finally {
		setBusy("");
	}
	emit("saved", sync.value);
	setApplyResult(describeOutcome(outcome));
	// A failed apply keeps the panel and everything typed into it exactly where it
	// was, so the customer can fix the cause without re-entering a key. Success (and
	// a timeout, where the config IS saved and still landing) re-seeds from the
	// server, which also closes the panel - the row is done.
	if (outcome.kind !== "failed") await load({ carry: pending });
	// The blocking wait gave up but the job did not: keep watching, slowly, so the
	// "it will finish on its own" message above becomes true on THIS screen rather
	// than only after the pane is closed and reopened. Started after load() because
	// load() starts the fast poller for a still-pending sync, and only one observer
	// may run at a time.
	if (outcome.kind === "pending") startBackgroundWatch();
	return { persisted: true, outcome };
}

// What an apply outcome says to the customer.
function describeOutcome(outcome) {
	if (outcome.kind === "failed") {
		return {
			kind: "failed",
			text: "Could not apply this to your agent.",
			detail: outcome.detail,
		};
	}
	if (outcome.kind === "ok") {
		return { kind: "ok", text: "Applied. Your agent is using it now.", detail: "" };
	}
	// Timed out, or the status endpoint stopped answering. Not an error: the job is
	// still running and a server-side reconciler catches anything it drops, so say
	// so plainly instead of showing a red state for something that is going fine.
	return {
		kind: "pending",
		text: "Still applying. This can take a minute, and it will finish on its own.",
		detail: "",
	};
}
// A success message is worth a glance, not a permanent fixture, so it retires
// itself. Anything the customer may still need to act on stays until the next
// apply replaces it.
let applyResultTimer = null;
function setApplyResult(result) {
	clearTimeout(applyResultTimer);
	applyResult.value = result;
	if (result && result.kind === "ok") {
		applyResultTimer = setTimeout(() => {
			if (applyResult.value === result) applyResult.value = null;
		}, 6000);
	}
}
const applyMessage = computed(() => {
	const r = applyResult.value;
	if (!r) return "";
	return r.detail ? `${r.text} ${r.detail}` : r.text;
});
// The one line at the foot of the editor. Prefers the outcome of the apply THIS
// editor just ran; falls back to whatever the server last recorded, which is what
// covers an apply still landing from a previous visit or started in another tab.
// Null hides the strip rather than leaving a bordered, empty band.
const statusLine = computed(() => {
	const r = applyResult.value;
	// A failure is already reported inside the open panel, right next to the row it
	// belongs to. Do not say it twice.
	if (r && !(r.kind === "failed" && panel.value.open)) {
		return { kind: r.kind, text: applyMessage.value };
	}
	// jarvis#809: applyStatus falls back to the durable server field
	// last_sync_status, so a plain "ok" ("Applied") kept resurfacing on every Settings
	// open even when nothing was applied this session. The transient success-after-a-
	// real-apply arrives via applyResult (describeOutcome) above, not this fallback, so
	// dropping the plain ok here retires the stale badge while keeping that flash. Failed
	// + Resync (jarvis#714), Disconnected/warn (jarvis#574) and pending still surface.
	if (applyStatus.value.kind !== "idle" && applyStatus.value.kind !== "ok")
		return applyStatus.value;
	return null;
});
// True exactly when the empty/disconnected box (rows list, !singleMode only -
// onboarding always seeds a row, see load()) is on screen. That box already
// shows applyStatus's own text, so the savebar below repeating the identical
// pill would be the same duplication busy.active already avoids for "Applying
// to your agent..." above. Read by the savebar's v-if, which still shows a
// failed status regardless: its Resync button lives only there, and a failed
// apply is not what put the pool in this empty state, so hiding it here would
// bury the one apply that still needs a retry.
const emptyBoxShowing = computed(
	() => !singleMode.value && !rows.value.length && !showDirectRow.value && !panel.value.open
);

// The ONE sync poller. Fire-and-forget callers (load, save) ignore the return
// value and get the old behaviour: refresh sync.value every few seconds until the
// apply settles. A caller that is BLOCKING on the apply awaits the promise, which
// resolves with the humanised outcome, and passes a deadline so it can hand the
// wait back instead of holding the customer hostage to a slow fleet.
//
// setTimeout, not setInterval: getLlmSyncStatus takes real time, and an interval
// would stack overlapping requests on a slow connection.
function startPolling(opts = {}) {
	stopPolling();
	// Exactly one observer of the sync status runs at a time. A new apply supersedes
	// the slow post-deadline watch below, and nothing has to reason about two pollers
	// writing sync.value (or two results racing onto the status strip).
	stopBackgroundWatch();
	const deadline = opts.timeoutMs ? Date.now() + opts.timeoutMs : 0;
	return new Promise((resolve) => {
		pollSettle = resolve;
		const tick = async () => {
			pollTimer = null;
			try {
				sync.value = (await api.getLlmSyncStatus()) || sync.value;
			} catch (e) {
				// A status call that fails says nothing about the apply itself, so
				// report "still going" rather than inventing a failure.
				settlePolling({ kind: "pending", text: "", detail: "" });
				return;
			}
			const st = humaniseSyncStatus(sync.value.last_sync_status);
			if (!sync.value.pending && st.kind !== "pending") {
				settlePolling(st);
				return;
			}
			if (deadline && Date.now() >= deadline) {
				settlePolling({ kind: "pending", text: "", detail: "" });
				return;
			}
			pollTimer = setTimeout(tick, POLL_MS);
		};
		// First read immediately: on_update writes "pending: …" inside the save
		// request, so the status is already meaningful the moment save returns.
		pollTimer = setTimeout(tick, 0);
	});
}
function stopPolling() {
	if (pollTimer) {
		clearTimeout(pollTimer);
		pollTimer = null;
	}
	// Anyone awaiting an abandoned poll is told the apply is still running, which is
	// true: stopping the poller does not stop the background job. Without this an
	// unmount (or a second startPolling) would strand the awaiting Connect forever
	// with the overlay up.
	settlePolling(null);
}
function settlePolling(outcome) {
	const resolve = pollSettle;
	if (!resolve) return;
	pollSettle = null;
	resolve(outcome || { kind: "pending", text: "", detail: "" });
}

// ---- post-deadline watch --------------------------------------------------
// APPLY_TIMEOUT_MS releases the customer, not the job: the enqueued apply is still
// running, and the status strip is left saying "Still applying. This can take a
// minute, and it will finish on its own." Nothing used to update it after that, so
// the promise was false for the very screen the sentence was on - the strip froze
// until the pane was closed and reopened. This keeps observing, slowly, and replaces
// the sentence with what actually happened.
//
// Nothing is blocked on this, so it polls far less often than POLL_MS.
const BG_POLL_MS = 15000;
// ...and it is bounded. An apply that has not settled in ten minutes will not be
// resolved by one more status read, and an observer with no end is a leak dressed as
// a feature. When the bound is reached the strip keeps the honest "it will finish on
// its own" line, and reopening the pane picks up whatever the server ended on.
const BG_POLL_MAX_MS = 600000;
let bgTimer = null;

function startBackgroundWatch() {
	// One observer at a time (load() may have started the fast poller for this same
	// still-pending apply) and never two of these.
	stopPolling();
	stopBackgroundWatch();
	const until = Date.now() + BG_POLL_MAX_MS;
	const tick = async () => {
		bgTimer = null;
		let failed = false;
		try {
			sync.value = (await api.getLlmSyncStatus()) || sync.value;
		} catch (e) {
			// A status read that fails says nothing about the apply, so keep watching
			// rather than inventing an outcome.
			failed = true;
		}
		if (!failed) {
			const st = humaniseSyncStatus(sync.value.last_sync_status);
			if (!sync.value.pending && st.kind !== "pending") {
				// The whole point: the strip stops claiming the apply is still running.
				// Deliberately no load() here - the pool was already written and reseeded
				// before this watch started, and a surprise reseed would close a panel the
				// customer has since opened.
				setApplyResult(describeOutcome(st));
				return;
			}
		}
		if (Date.now() >= until) return;
		bgTimer = setTimeout(tick, BG_POLL_MS);
	};
	bgTimer = setTimeout(tick, BG_POLL_MS);
}
function stopBackgroundWatch() {
	if (bgTimer) {
		clearTimeout(bgTimer);
		bgTimer = null;
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
onBeforeUnmount(() => {
	stopPolling();
	stopBackgroundWatch();
	clearTimeout(applyResultTimer);
	clearTimeout(subTestCoolTimer);
});

// Let a host (onboarding, footerless) drive Save from its own footer, and a
// hostScrim host (AiModelsPane) read the apply-in-flight state for its own
// scrim. AiModelsPane also mirrors busy.active into the shell store as
// settingsApplying, which both SettingsDialog's go() and the store's own
// openSettings() check before letting anything change settingsSection for
// the same duration (jarvis#821 review: a template ref alone only covered
// go(), not every writer of settingsSection).
// canStart / startBlockedReason let the onboarding controller (saveConnect) REQUIRE
// a savable+validated config - a connected subscription, a stored key, a local
// endpoint, or a freshly-typed remote key with a PASSING probe bound to it (P0-09) -
// before it opens an apply operation, and show a precise reason when it can't yet.
// subscriptionTesting: the host (OnboardingView) reads this to disable "Start
// chatting" while the subscription Test above is running, the other half of the
// hostBusy prop's mutual-exclusion guard.
defineExpose({
	save,
	busy,
	canStart: singleModeCanStart,
	startBlockedReason,
	subscriptionTesting: computed(() => subTest.value.testing),
});
</script>

<style scoped>
/* ===== Blocking apply overlay =============================================
   The editor is the positioning context so the scrim covers exactly it, not the
   whole settings dialog: the customer should still be able to close the dialog
   while their agent restarts in the background, so a hung apply never traps
   them. Inside the editor, though, nothing is clickable and (thanks to `inert`
   on the blocks underneath) nothing is tabbable either.

   Moving to another settings section while an apply is active is now BLOCKED,
   not allowed. That guard lives up in the shell store (settingsApplying),
   which AiModelsPane sets by watching this `busy` ref through the poolEditor
   template ref; SettingsDialog's rail and the store's own openSettings() both
   refuse to change section while it is true, so nothing that can unmount this
   editor mid-apply is left uncovered. This file has no notion of sibling
   panes, so it only ever owns the close affordance above, never the lock.

   hostScrim consumers (AiModelsPane) skip this box entirely and render their
   own wider one instead - the editor alone was too narrow a box: the pane's
   own save-bar status line sits below it and stayed sharp underneath a scrim
   scoped only to .jv-llm-editor (jarvis#559). Onboarding and ChatView never
   pass hostScrim, so they keep exactly this behaviour. ========================================= */
.jv-llm-editor {
	position: relative;
}
.jv-llm-busy {
	position: absolute;
	inset: 0;
	z-index: 5;
	display: grid;
	place-items: center;
	padding: 24px;
	border-radius: 10px;
	/* Translucent, not opaque: the row being connected and its own spinning
	   button stay legible through it, so the wait is attached to the thing the
	   customer pressed rather than floating over a blanked-out panel. */
	background: color-mix(in srgb, var(--surface) 78%, transparent);
	backdrop-filter: blur(1.5px);
}
/* color-mix is recent enough that a fallback is worth the two lines: without an
   opaque-ish backdrop the scrim would read as "nothing is happening". */
@supports not (background: color-mix(in srgb, red 50%, transparent)) {
	.jv-llm-busy {
		background: var(--surface);
		opacity: 0.88;
	}
}

/* Status strip (formerly the save bar). settings.css supplies the sink-to-bottom
   margin, the top border and the padding via .jv-pane-fill; this is only the
   internal layout the inline styles used to carry. */
.jv-pool-savebar {
	display: flex;
	align-items: center;
	gap: 12px;
	flex-wrap: wrap;
	justify-content: flex-end;
}
/* "Applied" is a word and belongs in a lozenge. A failure reason and the
   still-applying note are SENTENCES, so those two relax into a rounded rectangle
   that wraps, rather than stretching a pill the width of the pane. */
.jv-pool-savebar .jv-pool-syncpill--failed,
.jv-pool-savebar .jv-pool-syncpill--pending {
	align-items: flex-start;
	max-width: 100%;
	padding: 7px 11px;
	border-radius: 10px;
	line-height: 1.5;
	text-align: left;
}
.jv-pool-savebar .jv-pool-syncpill--failed .jv-pool-syncpill-ic,
.jv-pool-savebar .jv-pool-syncpill--pending .jv-pool-syncpill-ic {
	margin-top: 3px;
}

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
	/* flex: 1 1 0% (not the flex:none every neighbour on this row uses) so its
	   HYPOTHETICAL size for the wrap-fitting algorithm is 0, not its full
	   un-ellipsized text width. Without this, the row's flex-wrap safety net
	   (meant for genuinely narrow viewports) fired even at this pane's own
	   MAXIMUM possible width - see the jarvis#714 PR body for the measurement.
	   max-width: max-content caps the grow side at the same width: with plain
	   flex:1 the span consumed every pixel of the row's free space, dragging
	   "key set" / the health dot / the health label away from the model name
	   they describe. Capped, that free space now sinks into
	   .jv-flist-acts's own margin-left:auto instead, which is exactly where it
	   went before this row ever grew a fifth action button. */
	flex: 1 1 0%;
	max-width: max-content;
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
/* Connected-account identity on a collapsed subscription row - a muted sub-label,
   not a chip, so it reads as detail on the model name rather than a second badge.
   Capped and ellipsized like its neighbours (.jv-flist-model, .jv-pool-acct-health)
   so a long email can never push the row's actions off the edge. */
.jv-flist-acct {
	flex: none;
	max-width: 160px;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-size: 11px;
	color: var(--text-3);
}

/* ---- grouped subscription row (2+ accounts) -----------------------------
   .jv-flist-group wraps a model row (.jv-flist-row--grouped, bottom edge
   squared off) plus one .jv-flist-subrow per account - the pair reads as one
   bordered card instead of two separate rows. Only the last sub-row closes
   the card (rounded bottom corners); the group wrapper carries the bottom
   margin the single .jv-flist-row would otherwise supply itself. The group
   is the :hover target (not the individual rows) precisely so hovering
   anywhere on the card highlights the model row AND every sub-row together -
   a bare sibling hover can't reach backwards from a sub-row to the model
   row above it. */
.jv-flist-group {
	margin-bottom: 8px;
}
.jv-flist-row--grouped {
	margin-bottom: 0;
	border-bottom: none;
	border-bottom-left-radius: 0;
	border-bottom-right-radius: 0;
	transition: border-color 0.15s;
}
.jv-flist-subrow {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 6px 11px;
	border: 1px solid var(--border);
	border-top: none;
	background: var(--surface-2);
	transition: border-color 0.15s;
}
.jv-flist-subrow--last {
	border-bottom-left-radius: 9px;
	border-bottom-right-radius: 9px;
}
.jv-flist-group:hover .jv-flist-row--grouped,
.jv-flist-group:hover .jv-flist-subrow {
	border-color: var(--border-2);
}
@media (prefers-reduced-motion: reduce) {
	.jv-flist-row--grouped,
	.jv-flist-subrow {
		transition: none;
	}
}
/* Fixed-width tree glyph column, sized to land under the model row's badge
   (22px badge + 9px gap ≈ this column) so the sub-row identity lines up
   visually beneath the model it belongs to. */
.jv-flist-subrow-indent {
	flex: none;
	width: 31px;
	text-align: center;
	font-size: 12px;
	line-height: 1;
	color: var(--border-2);
	font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.jv-flist-subrow-avatar {
	flex: none;
	width: 18px;
	height: 18px;
	border-radius: 50%;
	background: var(--cta-bg);
	color: var(--cta);
	font-size: 9px;
	font-weight: 700;
	display: grid;
	place-items: center;
}
.jv-flist-subrow-email {
	flex: 1 1 auto;
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-size: 12.5px;
	color: var(--text);
}
/* account[0] is "primary", the rest "backup" - reflects the sticky rotation
   (always sticky; the rotation control itself was removed from the UI, see
   panel.mode's own comment). The label is read-only; reordering is done with the
   Up/Down arrows beside it (moveAccount, jarvis#807), not by clicking the label. */
.jv-flist-subrow-order {
	flex: none;
	font-size: 10.5px;
	font-weight: 600;
	text-transform: uppercase;
	letter-spacing: 0.03em;
	color: var(--text-3);
}
.jv-flist-subrow-acts {
	flex: none;
	margin-left: auto;
	display: flex;
	align-items: center;
	gap: 6px;
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
/* JvCombo's own scoped style draws a heavy `outline: 2px solid var(--cta)`
   ring on `:focus-visible` (border-color above is its only meant-for-here
   focus cue). Chromium matches :focus-visible on a real <input> even for a
   mouse click, so the Model field (allowCustom, has an inner <input>) shows
   that ring on click while the Provider field (a role=button div, no inner
   input) does not - the "Model's border is heavier than Provider/API key"
   look. Suppress it here so every field in this grid focuses identically. */
.jv-cfg-grid :deep(.jvc-field:focus-visible),
.jv-cfg-grid :deep(.jvc-field:has(.jvc-input:focus-visible)) {
	outline: none;
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

/* Empty/disconnected box (see the template note above it). Framed like
   .jv-cfgpanel below so it reads as an intentional state, not a stray line of
   text -- message centered, action directly under it, nothing new invented:
   the pill is .jv-pool-syncpill verbatim and the button is .jv-flist-addbtn
   verbatim, just laid out centered instead of end-aligned. */
.jv-flist-empty {
	display: flex;
	flex-direction: column;
	align-items: center;
	text-align: center;
	gap: 10px;
	padding: 22px 16px;
	border: 1px solid var(--border-2);
	border-radius: 11px;
	background: var(--surface);
}
.jv-flist-empty__msg {
	margin: 0;
	max-width: 46ch;
	font-size: 13px;
	line-height: 1.55;
	color: var(--text-3);
}
/* "+ Add a model" is a .jv-btn (see the template note): only its spacing and the
   plus glyph's tint are local. Everything else — height, radius, type, hover — comes
   from the shared button system, so it can never drift from Edit / Remove / Save. */
.jv-flist-addbtn {
	margin-top: 10px;
	gap: 6px;
}
/* Inside the centered empty box the button already sits under the message
   with the box's own `gap`, so the button's own top margin (needed for the
   end-aligned case below, where nothing else provides that spacing) would
   just double it. */
.jv-flist-empty .jv-flist-addbtn {
	margin-top: 0;
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
/* Sits directly under the list it refers to, and matches .jv-flist-hint's frame so
   the two never look like different kinds of message. Unlike the hint it carries an
   action, so it is a row with the button pushed to the end. */
.jv-flist-orderbar {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 12px;
	flex-wrap: wrap;
	margin-top: 14px;
	padding: 9px 11px 9px 13px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface-1);
}
.jv-flist-orderbar__msg {
	font-size: 12.5px;
	line-height: 1.55;
	color: var(--text-2);
}

.jv-flist-hint {
	display: flex;
	align-items: flex-start;
	gap: 9px;
	/* auto, not a fixed gap: sinks the hint to the bottom of the section's own
	   flex column (see the section's inline flex styles) so it settles into the
	   dialog's otherwise-empty space instead of hugging the row list above it. */
	margin-top: auto;
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
/* "Could not test from here": the probe never reached the endpoint, so this is
   not a verdict on the customer's key and must not wear the failure colour
   (#680). Amber is the same "look at this, nothing is broken yet" the sync pill
   and the account-health dot already use in this editor. */
.jv-status-warn {
	border: 1px solid var(--amber-bd);
	background: var(--amber-bg);
}
.jv-status-ic {
	flex: none;
	display: flex;
	align-self: flex-start;
	margin-top: 2px;
	color: var(--green);
}
.jv-status-bad .jv-status-ic {
	color: var(--red);
}
.jv-status-warn .jv-status-ic {
	color: var(--amber);
}
/* Wraps rather than ellipsing. This block used to be one nowrap line, which was
   survivable while it only ever said "Key works."/"Test failed.", but the text
   that now matters most is the explanation of an unreachable endpoint - and a
   truncated explanation is the same dead end #680 is about. Provider errors
   (the GLM balance message) stop being clipped for the same reason. */
.jv-status-tx {
	min-width: 0;
	color: var(--text);
}
.jv-status-tx b {
	color: var(--green);
	font-weight: 600;
}
.jv-status-bad .jv-status-tx b {
	color: var(--red);
}
.jv-status-warn .jv-status-tx b {
	color: var(--amber);
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
