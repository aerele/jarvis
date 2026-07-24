<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs
					:items="[
						{ label: 'Skills', route: { name: 'SkillsList' } },
						{ label: 'Review' },
					]"
				/>
			</template>
			<template #right-header>
				<Button
					v-if="!selfHosted"
					icon="refresh-cw"
					variant="ghost"
					:tooltip="'Refresh'"
					:loading="board.loading || decided.loading"
					@click="reloadAll"
				/>
			</template>
		</LayoutHeader>

		<!-- self-host: feature fully disabled (plan §13.3 / §7 T5) -->
		<div
			v-if="selfHosted"
			class="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center"
		>
			<FeatherIcon name="cloud-off" class="size-8 text-ink-gray-5" />
			<span class="text-lg font-medium text-ink-gray-8">
				Behavioural learning is available on managed plans
			</span>
			<span class="max-w-md text-p-base text-ink-gray-6">
				Pattern learning mines this site's history into reviewable defaults. It runs only
				on Jarvis-managed benches and is disabled on self-hosted installs.
			</span>
		</div>

		<!-- two panes at xl+ (left ~60% decision queue · right ~40% decided log);
		     stacked below xl so nothing overlaps at laptop widths -->
		<div v-else class="min-h-0 flex-1 overflow-y-auto">
			<div class="mx-auto grid w-full max-w-7xl grid-cols-1 gap-6 px-5 py-5 xl:grid-cols-5">
				<!-- ══════════════ LEFT · To review ══════════════ -->
				<section class="flex min-w-0 flex-col gap-4 xl:col-span-3">
					<!-- queue-type chips (Skills-area rework): additive, at the top of the
					     LEFT pane, same Button chip idiom as the domain facets below. The
					     accepted two-pane structure is otherwise untouched. The Promotions
					     count is its Pending list total (seeded from the access probe). -->
					<div class="flex flex-wrap items-center gap-2">
						<Button
							label="Skill candidates"
							:variant="queueType === 'candidates' ? 'solid' : 'subtle'"
							@click="setQueueType('candidates')"
						/>
						<Button
							:label="promo.total ? `Promotions · ${promo.total}` : 'Promotions'"
							:variant="queueType === 'promotions' ? 'solid' : 'subtle'"
							@click="setQueueType('promotions')"
						/>
						<Button
							:label="
								skillPromo.total
									? `Skill promotions · ${skillPromo.total}`
									: 'Skill promotions'
							"
							:variant="queueType === 'skillpromotions' ? 'solid' : 'subtle'"
							@click="setQueueType('skillpromotions')"
						/>
					</div>

					<!-- ────────── Skill candidates (the existing board, unchanged) ────────── -->
					<template v-if="queueType === 'candidates'">
						<!-- Apply bar. Stays mounted while an apply is in flight (applyActive)
					     even after the pending count hits 0, so the SyncPill's push
					     progress / failure never unmounts mid-push. -->
						<div
							v-if="pendingApplyCount > 0 || applyActive"
							class="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-outline-gray-2 bg-surface-gray-1 p-4"
						>
							<div class="flex items-center gap-2">
								<FeatherIcon name="upload-cloud" class="size-4 text-ink-gray-6" />
								<span
									v-if="pendingApplyCount > 0"
									class="text-base text-ink-gray-8"
								>
									{{ pendingApplyCount }} approved change{{
										pendingApplyCount === 1 ? "" : "s"
									}}
									pending
								</span>
								<span v-else class="text-base text-ink-gray-8"
									>Applying learned skills</span
								>
								<!-- stale-but-still-pushed patterns leave the assistant on Apply (§6.5) -->
								<span v-if="staleRemovalCount > 0" class="text-sm text-ink-gray-6">
									· {{ staleRemovalCount }} stale pattern{{
										staleRemovalCount === 1 ? "" : "s"
									}}
									will be removed
								</span>
								<SyncPill ref="syncPill" />
							</div>
							<Button
								v-if="pendingApplyCount > 0"
								variant="solid"
								label="Apply learned skills"
								iconLeft="upload-cloud"
								:loading="applyActive"
								@click="applyLearned"
							/>
						</div>

						<!-- pane header: honest math up front - the tab badge counts surfaced
					     proposals only, but queued rows are fully actionable here too, so
					     both numbers are said out loud (site-wide, filter-independent) -->
						<div class="flex flex-wrap items-center justify-between gap-2">
							<div class="min-w-0">
								<div class="text-base font-semibold text-ink-gray-9">
									To review
								</div>
								<div class="text-sm text-ink-gray-5">
									{{ toReviewCount }} to review · {{ queuedCount }} queued
								</div>
							</div>
							<div class="flex flex-wrap items-center gap-2">
								<FormControl
									type="text"
									class="w-48"
									placeholder="Search patterns"
									:modelValue="boardSearch"
									@update:modelValue="(v) => (boardSearch = v)"
								>
									<template #prefix>
										<FeatherIcon
											name="search"
											class="size-4 text-ink-gray-5"
										/>
									</template>
								</FormControl>
								<div class="w-36">
									<FormControl
										type="select"
										:options="STATUS_OPTIONS"
										:modelValue="boardStatus"
										@update:modelValue="setBoardStatus"
									/>
								</div>
							</div>
						</div>

						<div v-if="reviewActivity.total" class="text-sm text-ink-gray-5">
							{{ reviewActivity.decided }} of {{ reviewActivity.total }} surfaced
							proposals decided<template v-if="reviewActivity.last_by_name">
								· last by {{ reviewActivity.last_by_name }}</template
							>
						</div>

						<!-- domain facet chips (count-sorted by the server) -->
						<div class="flex flex-wrap items-center gap-2">
							<Button
								label="All domains"
								:variant="domain === '' ? 'solid' : 'subtle'"
								@click="setDomain('')"
							/>
							<Button
								v-for="f in facets"
								:key="f.value"
								:label="`${domainLabel(f.value)} · ${f.count}`"
								:variant="domain === f.value ? 'solid' : 'subtle'"
								@click="setDomain(f.value)"
							/>
						</div>

						<!-- batch-approve bar (A-class only) -->
						<div
							v-if="boardStatus === 'Proposed' && selectedNames.length"
							class="flex items-center justify-between gap-3 rounded-lg border border-outline-gray-2 bg-surface-gray-1 px-4 py-2"
						>
							<span class="text-sm text-ink-gray-7">
								{{ selectedNames.length }} aggregate-safe pattern{{
									selectedNames.length === 1 ? "" : "s"
								}}
								selected
							</span>
							<div class="flex items-center gap-2">
								<Button
									variant="ghost"
									label="Clear"
									@click="selected = new Set()"
								/>
								<Button
									variant="solid"
									theme="green"
									label="Approve selected"
									:loading="acting === '__batch__'"
									@click="doBatchApprove"
								/>
							</div>
						</div>

						<!-- cards -->
						<div v-if="board.loading && !board.rows.length" class="py-10 text-center">
							<LoadingIndicator class="size-5 text-ink-gray-5" />
						</div>
						<div
							v-else-if="!board.rows.length"
							class="flex flex-col items-center gap-1 rounded-lg border border-dashed py-14 text-center"
						>
							<FeatherIcon name="inbox" class="size-7 text-ink-gray-5" />
							<span class="mt-1 text-base font-medium text-ink-gray-8">{{
								emptyTitle
							}}</span>
							<span class="text-p-base text-ink-gray-6">{{ emptyDescription }}</span>
						</div>

						<div v-else class="flex flex-col gap-3">
							<!-- one group-level sensitivity note replaces the per-card banner
						     wallpaper; the full role-specific text shows on expand -->
							<div
								v-if="hasActionableB"
								class="flex items-start gap-2 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
							>
								<FeatherIcon
									name="alert-triangle"
									class="mt-0.5 size-4 shrink-0"
								/>
								<span>
									Rows marked <b>B</b> contain exact text every chat user on this
									site can read once approved - expand a row for its role
									details.
								</span>
							</div>
							<template v-for="group in rowGroups" :key="group.key">
								<!-- queued divider: not-yet-surfaced Proposed rows ride the same
							     fetch (surfaced=all, partitioned client-side) and keep full
							     actions - no separate "Review them now" detour anymore -->
								<div
									v-if="group.key === 'queued'"
									class="mt-1 flex items-center gap-2 text-sm text-ink-gray-5"
								>
									<span class="h-px flex-1 border-t"></span>
									<span>Queued - surfaces after the next analysis run</span>
									<span class="h-px flex-1 border-t"></span>
								</div>

								<div
									v-for="row in group.rows"
									:key="row.name"
									class="rounded-lg border p-4"
								>
									<!-- badges row -->
									<div class="flex flex-wrap items-center gap-1.5">
										<Checkbox
											v-if="
												boardStatus === 'Proposed' &&
												row.effective_sensitivity === 'A'
											"
											:modelValue="selected.has(row.name)"
											@update:modelValue="() => toggleSelect(row)"
										/>
										<Badge
											variant="subtle"
											:theme="strengthTheme(row.strength_band)"
											:label="row.strength_band || 'Low'"
										/>
										<Badge
											variant="outline"
											theme="gray"
											:label="domainLabel(row.domain)"
										/>
										<Badge
											v-if="row.company"
											variant="outline"
											theme="gray"
											:label="row.company"
										/>
										<Badge
											variant="subtle"
											:theme="sensBadge(row).theme"
											:label="sensBadge(row).label"
										/>
										<Badge
											v-if="appliedSkill(row)"
											variant="subtle"
											theme="green"
											:label="`Applied to skill: ${appliedSkill(row)}`"
										/>
										<Badge
											v-else-if="isAcknowledged(row)"
											variant="subtle"
											theme="gray"
											label="Acknowledged"
										/>
										<Badge
											v-else-if="boardStatus !== 'Proposed'"
											variant="subtle"
											:theme="STATUS_THEME[row.status] || 'gray'"
											:label="row.status"
										/>
										<!-- caveats -->
										<Tooltip
											v-if="row.has_overlap_warning"
											:text="row.overlap_warning"
										>
											<Badge
												variant="subtle"
												theme="orange"
												label="Overlaps a custom skill"
											/>
										</Tooltip>
										<Tooltip
											v-if="row.exceptions_cluster"
											:text="row.exceptions_cluster"
										>
											<Badge
												variant="subtle"
												theme="blue"
												label="Possible sub-rule"
											/>
										</Tooltip>
										<Badge
											v-if="row.not_applicable"
											variant="subtle"
											theme="gray"
											label="Not applicable"
										/>
										<Badge
											v-if="row.draft_edited"
											variant="subtle"
											theme="gray"
											label="Edited"
										/>
										<!-- correction loop (§6.5): chat users flagged this default as wrong -->
										<Tooltip
											v-if="row.flags_count"
											:text="`Chat users flagged this default as wrong ${
												row.flags_count
											} time${row.flags_count === 1 ? '' : 's'}.`"
										>
											<Badge
												variant="subtle"
												theme="red"
												:label="`${row.flags_count} flag${
													row.flags_count === 1 ? '' : 's'
												}`"
											/>
										</Tooltip>
									</div>

									<!-- plain-English pattern sentence -->
									<p class="mt-2.5 text-base text-ink-gray-9">
										{{ row.pattern_statement }}
									</p>

									<!-- linked Personalise question (Skills-area rework): the reviewer
								     decides with the human context - did the user answer the question
								     this pattern raised, and what did they say. Enriched server-side
								     (question_status / question_answer_excerpt), never a per-row fetch. -->
									<div v-if="row.question_status" class="mt-2">
										<div
											class="flex items-center gap-1.5 text-sm text-ink-gray-6"
										>
											<FeatherIcon
												name="message-circle"
												class="size-3.5 shrink-0"
											/>
											<span>Asked the user · {{ row.question_status }}</span>
										</div>
										<div
											v-if="row.question_answer_excerpt"
											class="mt-1.5 rounded bg-surface-gray-1 px-3 py-2 text-sm italic text-ink-gray-7"
										>
											“{{ row.question_answer_excerpt }}”
										</div>
									</div>

									<div
										class="mt-1 flex flex-wrap items-center gap-x-2 text-sm text-ink-gray-5"
									>
										<span v-if="row.exception_n">
											{{ row.exception_n }} known exception{{
												row.exception_n === 1 ? "" : "s"
											}}
										</span>
										<span v-if="row.approved_by"
											>· Approved by {{ row.approved_by }}</span
										>
										<span v-else-if="row.reviewed_by"
											>· Reviewed by {{ row.reviewed_by }}</span
										>
									</div>

									<!-- Stale banner (§6.5): drift re-validation found the habit no longer
								     holds. Never a silent edit: the SM re-approves (Approve) or rejects;
								     a still-pushed row is removed from the assistant on the next Apply. -->
									<div
										v-if="row.status === 'Stale'"
										class="mt-2.5 flex items-start gap-2 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
									>
										<FeatherIcon
											name="trending-down"
											class="mt-0.5 size-4 shrink-0"
										/>
										<span>
											{{
												row.stale_reason ||
												"This pattern no longer holds in recent data."
											}}
											<template v-if="row.materialized_skill">
												Still pushed to the assistant; it will be removed
												on the next Apply unless re-approved.
											</template>
										</span>
									</div>

									<!-- B-class exact-text disclosure banner (plan §6.6): learned skill
								     files are bench-global, so role scoping steers activation but is
								     not a confidentiality boundary. -->
									<!-- shown on the EXPANDED decidable card only: repeated verbatim on
								     every collapsed B row it trains the exact banner blindness it
								     warns about (a group-level note above the list covers the rest) -->
									<div
										v-if="
											row.effective_sensitivity === 'B' &&
											['Proposed', 'Stale', 'Snoozed'].includes(
												row.status
											) &&
											expanded[row.name]
										"
										class="mt-2.5 flex items-start gap-2 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
									>
										<FeatherIcon
											name="alert-triangle"
											class="mt-0.5 size-4 shrink-0"
										/>
										<span>{{ disclosureText(bRoles(row)) }}</span>
									</div>

									<!-- drill-down toggle -->
									<button
										class="mt-2 flex items-center gap-1 text-sm text-ink-gray-6 hover:text-ink-gray-8"
										@click="toggleExpand(row.name)"
									>
										<FeatherIcon
											:name="
												expanded[row.name]
													? 'chevron-down'
													: 'chevron-right'
											"
											class="size-3.5"
										/>
										Evidence &amp; details
									</button>

									<div v-if="expanded[row.name]" class="mt-3 border-t pt-3">
										<div
											v-if="expanded[row.name] === 'loading'"
											class="py-4 text-center"
										>
											<LoadingIndicator class="size-4 text-ink-gray-5" />
										</div>
										<template v-else>
											<div
												v-if="expanded[row.name].frozen_evidence_label"
												class="mb-3 rounded border border-outline-gray-2 bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-6"
											>
												{{ expanded[row.name].frozen_evidence_label }}
											</div>

											<!-- note-sourced insights have no table statistics; the zero
										     grid reads as "no basis" when the basis is a voice note -->
											<div
												v-if="
													expanded[row.name].detector_id ===
													'voice-context'
												"
												class="text-sm text-ink-gray-6"
											>
												From spoken or written business notes - table
												statistics don't apply to this kind of insight.
											</div>
											<!-- raw stats -->
											<div
												v-else
												class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm sm:grid-cols-3"
											>
												<div>
													<span class="text-ink-gray-5">Confidence</span>
													<span class="ml-1.5 text-ink-gray-8">{{
														pct(expanded[row.name].confidence_pct)
													}}</span>
												</div>
												<div>
													<span class="text-ink-gray-5">Support</span>
													<span class="ml-1.5 text-ink-gray-8"
														>{{
															expanded[row.name].support_n
														}}
														units</span
													>
												</div>
												<div>
													<span class="text-ink-gray-5">Rows</span>
													<span class="ml-1.5 text-ink-gray-8">{{
														expanded[row.name].n_rows
													}}</span>
												</div>
												<div>
													<span class="text-ink-gray-5">Wilson low</span>
													<span class="ml-1.5 text-ink-gray-8">{{
														pct(mul100(expanded[row.name].wilson_low))
													}}</span>
												</div>
												<div>
													<span class="text-ink-gray-5"
														>Gap vs base</span
													>
													<span class="ml-1.5 text-ink-gray-8">{{
														pct(mul100(expanded[row.name].gap))
													}}</span>
												</div>
												<div>
													<span class="text-ink-gray-5">Exceptions</span>
													<span class="ml-1.5 text-ink-gray-8">{{
														expanded[row.name].exception_n
													}}</span>
												</div>
												<div v-if="expanded[row.name].last_validated_at">
													<span class="text-ink-gray-5"
														>Last validated</span
													>
													<Tooltip
														:text="
															exactDate(
																expanded[row.name]
																	.last_validated_at
															)
														"
													>
														<span class="ml-1.5 text-ink-gray-8">{{
															timeAgo(
																expanded[row.name]
																	.last_validated_at
															)
														}}</span>
													</Tooltip>
												</div>
											</div>

											<!-- role chips -->
											<div
												v-if="(expanded[row.name].roles || []).length"
												class="mt-3 flex flex-wrap items-center gap-1.5"
											>
												<span class="text-sm text-ink-gray-5">Roles:</span>
												<Badge
													v-for="r in expanded[row.name].roles"
													:key="r"
													variant="outline"
													theme="gray"
													:label="r"
												/>
											</div>

											<!-- compiled bullet preview -->
											<div
												v-if="expanded[row.name].compiled_preview"
												class="mt-3"
											>
												<div class="mb-1 text-sm text-ink-gray-5">
													{{
														row.effective_sensitivity === "A"
															? "Compiled rule"
															: "Suggested instruction"
													}}
												</div>
												<pre
													class="overflow-x-auto whitespace-pre-wrap rounded border bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-8"
													>{{ expanded[row.name].compiled_preview }}</pre
												>
											</div>

											<!-- known exceptions (SM may see named parties) + Desk links -->
											<div
												v-if="(expanded[row.name].exceptions || []).length"
												class="mt-3"
											>
												<div class="mb-1 text-sm text-ink-gray-5">
													Known exceptions
												</div>
												<div class="flex flex-wrap gap-1.5">
													<a
														v-for="(ex, i) in expanded[row.name]
															.exceptions"
														:key="i"
														:href="exDeskUrl(ex)"
														:target="
															exDeskUrl(ex) ? '_blank' : undefined
														"
														rel="noopener"
														class="inline-flex items-center gap-1 rounded bg-surface-gray-2 px-2 py-0.5 text-sm text-ink-gray-8"
														:class="
															exDeskUrl(ex)
																? 'hover:bg-surface-gray-3'
																: 'cursor-default'
														"
													>
														{{ exLabel(ex) }}
														<FeatherIcon
															v-if="exDeskUrl(ex)"
															name="external-link"
															class="size-3 text-ink-gray-5"
														/>
													</a>
												</div>
											</div>

											<!-- Desk links: runs + materialized skill -->
											<div
												class="mt-3 flex flex-wrap items-center gap-3 text-sm"
											>
												<a
													v-if="expanded[row.name].last_seen_run"
													:href="
														deskUrl(
															'Jarvis Pattern Run',
															expanded[row.name].last_seen_run
														)
													"
													target="_blank"
													rel="noopener"
													class="inline-flex items-center gap-1 text-ink-gray-7 hover:underline"
												>
													<FeatherIcon
														name="activity"
														class="size-3.5"
													/>
													Run {{ expanded[row.name].last_seen_run }}
												</a>
												<a
													v-if="expanded[row.name].materialized_skill"
													:href="
														deskUrl(
															'Jarvis Custom Skill',
															expanded[row.name].materialized_skill
														)
													"
													target="_blank"
													rel="noopener"
													class="inline-flex items-center gap-1 text-ink-gray-7 hover:underline"
												>
													<FeatherIcon name="zap" class="size-3.5" />
													{{ expanded[row.name].materialized_skill }}
												</a>
												<span class="text-ink-gray-4">{{ row.name }}</span>
											</div>
										</template>
									</div>

									<!-- actions -->
									<div
										class="mt-3 flex flex-wrap items-center gap-2 border-t pt-3"
									>
										<template
											v-if="
												row.status === 'Proposed' || row.status === 'Stale'
											"
										>
											<!-- A-class: approvable + compiled + pushed -->
											<template v-if="row.effective_sensitivity === 'A'">
												<Button
													variant="solid"
													theme="green"
													label="Approve"
													:loading="acting === row.name"
													:disabled="!!acting"
													@click="doApprove(row)"
												/>
												<!-- direct skill edit (Skills-area rework): once the learned
											     skill exists, edit it directly (SkillDetail); before that,
											     Edit-and-approve tweaks the compiled draft as today. -->
												<Button
													v-if="row.materialized_skill"
													variant="subtle"
													label="Edit skill…"
													iconLeft="edit-3"
													:disabled="!!acting"
													@click="editSkill(row)"
												/>
												<Button
													v-else
													variant="subtle"
													label="Edit &amp; approve"
													:disabled="!!acting"
													@click="openEdit(row)"
												/>
												<Dropdown
													v-if="row.status === 'Proposed'"
													:options="snoozeOptions(row)"
												>
													<Button
														variant="subtle"
														label="Snooze"
														iconRight="chevron-down"
														:disabled="!!acting"
													/>
												</Dropdown>
												<Button
													variant="subtle"
													theme="red"
													label="Reject"
													:disabled="!!acting"
													@click="openReject(row)"
												/>
												<Button
													v-if="row.question_status"
													variant="subtle"
													label="Ask the user"
													iconLeft="help-circle"
													:disabled="!!acting"
													@click="openAsk(row.name)"
												/>
												<Button
													variant="ghost"
													label="Go to chat"
													iconLeft="message-circle"
													:disabled="!!acting"
													@click="goToChat('pattern', row.name, row)"
												/>
											</template>
											<!-- B/C: insight-only in Phase 1 (never compiled/pushed), so
										     Acknowledge (records reviewed) replaces Approve. "Apply to
										     skill…" (D5) folds the insight into an org custom skill via
										     an LLM-drafted, SM-confirmed update instead. -->
											<template v-else>
												<Button
													variant="subtle"
													label="Acknowledge (insight only)"
													:loading="acting === row.name"
													:disabled="!!acting"
													@click="doAcknowledge(row)"
												/>
												<Button
													variant="subtle"
													label="Apply to skill…"
													:disabled="!!acting"
													@click="openInsightApply(row)"
												/>
												<Dropdown
													v-if="row.status === 'Proposed'"
													:options="snoozeOptions(row)"
												>
													<Button
														variant="subtle"
														label="Snooze"
														iconRight="chevron-down"
														:disabled="!!acting"
													/>
												</Dropdown>
												<Button
													variant="subtle"
													theme="red"
													label="Reject"
													:disabled="!!acting"
													@click="openReject(row)"
												/>
												<Button
													v-if="row.question_status"
													variant="subtle"
													label="Ask the user"
													iconLeft="help-circle"
													:disabled="!!acting"
													@click="openAsk(row.name)"
												/>
												<Button
													variant="ghost"
													label="Go to chat"
													iconLeft="message-circle"
													:disabled="!!acting"
													@click="goToChat('pattern', row.name, row)"
												/>
												<span class="text-sm text-ink-gray-5">
													Insight only in Phase 1: recorded as reviewed,
													not pushed to the assistant.
												</span>
											</template>
										</template>
										<template v-else-if="row.status === 'Approved'">
											<Button
												variant="subtle"
												label="Un-approve"
												:loading="acting === row.name"
												:disabled="!!acting"
												@click="doUnapprove(row)"
											/>
											<span
												v-if="row.effective_sensitivity !== 'A'"
												class="text-sm text-ink-gray-5"
											>
												Approved but insight-only: not pushed to the
												assistant.
											</span>
										</template>
										<template v-else-if="row.status === 'Rejected'">
											<span
												v-if="isAcknowledged(row)"
												class="text-sm text-ink-gray-5"
											>
												Acknowledged as insight: reviewed, not pushed.
											</span>
											<Button
												variant="subtle"
												label="Restore"
												:loading="acting === row.name"
												:disabled="!!acting"
												@click="doRestore(row)"
											/>
										</template>
										<span v-else class="text-sm text-ink-gray-5"
											>No actions available.</span
										>
									</div>
								</div>
							</template>

							<!-- N of M + load more -->
							<div
								v-if="board.rows.length"
								class="flex flex-col items-center gap-2 pt-1"
							>
								<span class="text-sm text-ink-gray-5">
									{{ board.rows.length }} of {{ board.total }}
								</span>
								<Button
									v-if="board.hasMore"
									variant="subtle"
									label="Load more"
									:loading="board.loading"
									@click="fetchBoard('more')"
								/>
							</div>
						</div>

						<!-- queued rows exist but none are loaded/visible under the current
					     filter (e.g. a later page, or a domain chip hides them) -->
						<div
							v-if="boardStatus === 'Proposed' && queuedCount > 0 && !queuedVisible"
							class="text-sm text-ink-gray-5"
						>
							{{ queuedCount }} queued proposal{{
								queuedCount === 1 ? "" : "s"
							}}
							site-wide - they surface after the next analysis run.<template
								v-if="board.hasMore"
							>
								Load more to see them here.</template
							>
						</div>
					</template>

					<!-- ────────── Promotions (wiki User→Role/Org publish requests) ────────── -->
					<template v-else-if="queueType === 'promotions'">
						<div class="min-w-0">
							<div class="text-base font-semibold text-ink-gray-9">Promotions</div>
							<div class="text-sm text-ink-gray-5">
								Requests to publish a personal page to a role or the whole org
							</div>
						</div>

						<div v-if="promo.loading && !promo.rows.length" class="py-10 text-center">
							<LoadingIndicator class="size-5 text-ink-gray-5" />
						</div>
						<div
							v-else-if="!promo.rows.length"
							class="flex flex-col items-center gap-1 rounded-lg border border-dashed py-14 text-center"
						>
							<FeatherIcon name="git-pull-request" class="size-7 text-ink-gray-5" />
							<span class="mt-1 text-base font-medium text-ink-gray-8">
								No promotion requests
							</span>
							<span class="text-p-base text-ink-gray-6">
								Users can request promoting their personal pages from the Wiki.
							</span>
						</div>

						<div v-else class="flex flex-col gap-3">
							<div
								v-for="p in promo.rows"
								:key="`promo-${p.name}`"
								class="rounded-lg border p-4"
							>
								<!-- page title + from→to scope -->
								<div class="flex flex-wrap items-center gap-x-2 gap-y-1.5">
									<span class="text-base font-medium text-ink-gray-9">{{
										p.page_title
									}}</span>
									<span class="flex items-center gap-1.5">
										<Badge
											variant="subtle"
											theme="gray"
											:label="p.from_scope || 'User'"
										/>
										<FeatherIcon
											name="arrow-right"
											class="size-3.5 text-ink-gray-5"
										/>
										<Badge
											variant="subtle"
											theme="blue"
											:label="toScopeLabel(p)"
										/>
									</span>
								</div>

								<!-- requester + when -->
								<div
									class="mt-2 flex flex-wrap items-center gap-2 text-sm text-ink-gray-6"
								>
									<Avatar
										size="sm"
										:label="p.requested_by_name || p.requested_by || '?'"
									/>
									<span>{{ p.requested_by_name || p.requested_by }}</span>
									<template v-if="p.created">
										<span class="text-ink-gray-4">·</span>
										<Tooltip :text="exactDate(p.created)">
											<span>{{ timeAgo(p.created) }}</span>
										</Tooltip>
									</template>
								</div>

								<!-- requester's why -->
								<p v-if="p.note" class="mt-2 text-sm text-ink-gray-7">
									{{ p.note }}
								</p>

								<!-- body excerpt (expandable: un-clamps the excerpt) -->
								<div v-if="p.body_excerpt" class="mt-2">
									<div
										class="whitespace-pre-wrap rounded bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-7"
										:class="promoExpanded[p.name] ? '' : 'line-clamp-3'"
									>
										{{ p.body_excerpt }}
									</div>
									<button
										v-if="p.body_excerpt.length > 160"
										class="mt-1 text-sm text-ink-gray-6 hover:text-ink-gray-8"
										@click="togglePromoBody(p.name)"
									>
										{{ promoExpanded[p.name] ? "Show less" : "Show more" }}
									</button>
								</div>

								<!-- actions -->
								<div class="mt-3 flex flex-wrap items-center gap-2 border-t pt-3">
									<template v-if="p.status === 'Pending'">
										<Button
											variant="solid"
											theme="green"
											label="Approve"
											:loading="promoActing === p.name"
											:disabled="!!promoActing"
											@click="approvePromotion(p)"
										/>
										<Button
											variant="subtle"
											theme="red"
											label="Reject"
											:disabled="!!promoActing"
											@click="openPromoReject(p)"
										/>
										<Button
											variant="subtle"
											label="Ask the user"
											iconLeft="help-circle"
											:disabled="!!promoActing"
											@click="openAsk(p.name)"
										/>
										<Button
											variant="ghost"
											label="Go to chat"
											iconLeft="message-circle"
											:disabled="!!promoActing"
											@click="goToChat('promotion', p.name)"
										/>
									</template>
									<template v-else>
										<Badge
											variant="subtle"
											:theme="p.status === 'Approved' ? 'green' : 'red'"
											:label="p.status"
										/>
										<Button
											variant="ghost"
											label="Go to chat"
											iconLeft="message-circle"
											@click="goToChat('promotion', p.name)"
										/>
									</template>
								</div>
							</div>

							<!-- N of M + load more -->
							<div class="flex flex-col items-center gap-2 pt-1">
								<span class="text-sm text-ink-gray-5">
									{{ promo.rows.length }} of {{ promo.total }}
								</span>
								<Button
									v-if="promo.hasMore"
									variant="subtle"
									label="Load more"
									:loading="promo.loading"
									@click="fetchPromotions('more')"
								/>
							</div>
						</div>
					</template>

					<!-- ────────── Skill promotions (User→Role/Org widen requests) ────────── -->
					<template v-else>
						<div class="min-w-0">
							<div class="text-base font-semibold text-ink-gray-9">
								Skill promotions
							</div>
							<div class="text-sm text-ink-gray-5">
								Requests to widen a private skill to a role or the whole org
							</div>
						</div>

						<div
							v-if="skillPromo.loading && !skillPromo.rows.length"
							class="py-10 text-center"
						>
							<LoadingIndicator class="size-5 text-ink-gray-5" />
						</div>
						<div
							v-else-if="!skillPromo.rows.length"
							class="flex flex-col items-center gap-1 rounded-lg border border-dashed py-14 text-center"
						>
							<FeatherIcon name="git-pull-request" class="size-7 text-ink-gray-5" />
							<span class="mt-1 text-base font-medium text-ink-gray-8">
								No skill promotion requests
							</span>
							<span class="text-p-base text-ink-gray-6">
								Users request promoting a private skill from the skill's page.
							</span>
						</div>

						<div v-else class="flex flex-col gap-3">
							<div
								v-for="p in skillPromo.rows"
								:key="`skillpromo-${p.name}`"
								class="rounded-lg border p-4"
							>
								<!-- skill name + from→to scope -->
								<div class="flex flex-wrap items-center gap-x-2 gap-y-1.5">
									<span class="text-base font-medium text-ink-gray-9">{{
										p.skill_name
									}}</span>
									<span class="flex items-center gap-1.5">
										<Badge
											variant="subtle"
											theme="gray"
											:label="p.from_scope || 'User'"
										/>
										<FeatherIcon
											name="arrow-right"
											class="size-3.5 text-ink-gray-5"
										/>
										<Badge
											variant="subtle"
											theme="blue"
											:label="toScopeLabel(p)"
										/>
									</span>
								</div>

								<!-- requester + when -->
								<div
									class="mt-2 flex flex-wrap items-center gap-2 text-sm text-ink-gray-6"
								>
									<Avatar
										size="sm"
										:label="p.requested_by_name || p.requested_by || '?'"
									/>
									<span>{{ p.requested_by_name || p.requested_by }}</span>
									<template v-if="p.created">
										<span class="text-ink-gray-4">·</span>
										<Tooltip :text="exactDate(p.created)">
											<span>{{ timeAgo(p.created) }}</span>
										</Tooltip>
									</template>
								</div>

								<!-- requester's why -->
								<p v-if="p.note" class="mt-2 text-sm text-ink-gray-7">
									{{ p.note }}
								</p>

								<!-- skill instructions preview (expandable) -->
								<div v-if="p.body_excerpt" class="mt-2">
									<div
										class="whitespace-pre-wrap rounded bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-7"
										:class="skillPromoExpanded[p.name] ? '' : 'line-clamp-3'"
									>
										{{ p.body_excerpt }}
									</div>
									<button
										v-if="p.body_excerpt.length > 160"
										class="mt-1 text-sm text-ink-gray-6 hover:text-ink-gray-8"
										@click="toggleSkillPromoBody(p.name)"
									>
										{{
											skillPromoExpanded[p.name] ? "Show less" : "Show more"
										}}
									</button>
								</div>

								<!-- Org push-budget warning (loud, non-blocking): approval is still
								     allowed; the reviewer decides informed (ruling 2). -->
								<div
									v-if="p.status === 'Pending' && budgetWarn(p)"
									class="mt-3 rounded-lg border px-3 py-2 text-sm"
									:class="
										budgetWarn(p).level === 'over'
											? 'border-outline-red-2 bg-surface-red-1 text-ink-red-4'
											: 'border-outline-amber-2 bg-surface-amber-1 text-ink-amber-3'
									"
								>
									{{ budgetWarn(p).message }}
								</div>

								<!-- actions -->
								<div class="mt-3 flex flex-wrap items-center gap-2 border-t pt-3">
									<template v-if="p.status === 'Pending'">
										<Button
											variant="solid"
											theme="green"
											label="Approve"
											:loading="skillPromoActing === p.name"
											:disabled="!!skillPromoActing"
											@click="approveSkillPromotion(p)"
										/>
										<Button
											variant="subtle"
											theme="red"
											label="Reject"
											:disabled="!!skillPromoActing"
											@click="openSkillPromoReject(p)"
										/>
									</template>
									<template v-else>
										<Badge
											variant="subtle"
											:theme="p.status === 'Approved' ? 'green' : 'red'"
											:label="p.status"
										/>
									</template>
								</div>
							</div>

							<!-- N of M + load more -->
							<div class="flex flex-col items-center gap-2 pt-1">
								<span class="text-sm text-ink-gray-5">
									{{ skillPromo.rows.length }} of {{ skillPromo.total }}
								</span>
								<Button
									v-if="skillPromo.hasMore"
									variant="subtle"
									label="Load more"
									:loading="skillPromo.loading"
									@click="fetchSkillPromotions('more')"
								/>
							</div>
						</div>
					</template>
				</section>

				<!-- ══════════════ RIGHT · Decided log ══════════════ -->
				<!-- Compact who/when/what audit trail (view="decided"): one row per
				     human-touched terminal/parked pattern; click a row to expand the
				     same drill-down detail the queue uses. Disposition/sort/search
				     filter server-side (decided view only). -->
				<section class="flex min-w-0 flex-col gap-3 xl:col-span-2">
					<div class="flex flex-wrap items-center justify-between gap-2">
						<div class="flex items-baseline gap-1.5">
							<span class="text-base font-semibold text-ink-gray-9">Decided</span>
							<span class="text-sm text-ink-gray-5">{{ decided.total }}</span>
						</div>
						<div class="flex flex-wrap items-center gap-2">
							<FormControl
								type="text"
								class="w-40"
								placeholder="Search decided"
								:modelValue="decided.search"
								@update:modelValue="(v) => (decided.search = v)"
							>
								<template #prefix>
									<FeatherIcon name="search" class="size-4 text-ink-gray-5" />
								</template>
							</FormControl>
							<div class="w-36">
								<FormControl
									type="select"
									:options="DISPOSITION_OPTIONS"
									:modelValue="decided.disposition"
									@update:modelValue="setDisposition"
								/>
							</div>
							<Button
								variant="subtle"
								:label="decided.sort === 'oldest' ? 'Oldest' : 'Newest'"
								:iconLeft="decided.sort === 'oldest' ? 'arrow-up' : 'arrow-down'"
								:tooltip="'Sort by decided time'"
								@click="toggleDecidedSort"
							/>
						</div>
					</div>

					<div v-if="decided.loading && !decided.rows.length" class="py-8 text-center">
						<LoadingIndicator class="size-5 text-ink-gray-5" />
					</div>
					<div
						v-else-if="!decided.rows.length"
						class="flex flex-col items-center gap-1 rounded-lg border border-dashed px-4 py-10 text-center"
					>
						<FeatherIcon
							:name="decidedFiltered ? 'search' : 'check-circle'"
							class="size-7 text-ink-gray-5"
						/>
						<span class="mt-1 text-base font-medium text-ink-gray-8">
							{{ decidedFiltered ? "No matching decisions" : "No decisions yet" }}
						</span>
						<span class="text-p-base text-ink-gray-6">
							{{
								decidedFiltered
									? "Try a different search or disposition filter."
									: "Approvals, acknowledgements, rejections and snoozes land here."
							}}
						</span>
					</div>

					<div v-else class="rounded-lg border">
						<div
							v-for="row in decided.rows"
							:key="`decided-${row.name}`"
							class="border-b px-3 py-2.5 last:border-b-0"
						>
							<!-- compact row: excerpt + disposition + who/when; click to expand -->
							<button
								class="flex w-full items-start gap-2 text-left"
								@click="toggleDecidedExpand(row.name)"
							>
								<div class="min-w-0 flex-1">
									<p class="line-clamp-2 text-sm text-ink-gray-8">
										{{ excerpt(row.pattern_statement, 160) }}
									</p>
									<div
										class="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-ink-gray-5"
									>
										<Tooltip
											v-if="dispositionBadge(row).skill"
											:text="`Applied to skill: ${
												dispositionBadge(row).skill
											}`"
										>
											<Badge
												variant="subtle"
												theme="green"
												label="Applied"
											/>
										</Tooltip>
										<Badge
											v-else
											variant="subtle"
											:theme="dispositionBadge(row).theme"
											:label="dispositionBadge(row).label"
										/>
										<span v-if="row.reviewed_by">{{ row.reviewed_by }}</span>
										<template v-if="row.reviewed_at">
											<span class="text-ink-gray-4">·</span>
											<Tooltip :text="exactDate(row.reviewed_at)">
												<span>{{ timeAgo(row.reviewed_at) }}</span>
											</Tooltip>
										</template>
									</div>
								</div>
								<FeatherIcon
									:name="dExpanded[row.name] ? 'chevron-down' : 'chevron-right'"
									class="mt-0.5 size-3.5 shrink-0 text-ink-gray-5"
								/>
							</button>

							<!-- drill-down: same detail payload as the queue, separate cache
							     (dExpanded) so open state never couples across the panes -->
							<div v-if="dExpanded[row.name]" class="mt-2.5 border-t pt-2.5">
								<div
									v-if="dExpanded[row.name] === 'loading'"
									class="py-4 text-center"
								>
									<LoadingIndicator class="size-4 text-ink-gray-5" />
								</div>
								<template v-else>
									<div
										v-if="dExpanded[row.name].frozen_evidence_label"
										class="mb-3 rounded border border-outline-gray-2 bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-6"
									>
										{{ dExpanded[row.name].frozen_evidence_label }}
									</div>

									<div
										v-if="dExpanded[row.name].detector_id === 'voice-context'"
										class="text-sm text-ink-gray-6"
									>
										From spoken or written business notes - table statistics
										don't apply to this kind of insight.
									</div>
									<div v-else class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
										<div>
											<span class="text-ink-gray-5">Confidence</span>
											<span class="ml-1.5 text-ink-gray-8">{{
												pct(dExpanded[row.name].confidence_pct)
											}}</span>
										</div>
										<div>
											<span class="text-ink-gray-5">Support</span>
											<span class="ml-1.5 text-ink-gray-8"
												>{{ dExpanded[row.name].support_n }} units</span
											>
										</div>
										<div>
											<span class="text-ink-gray-5">Rows</span>
											<span class="ml-1.5 text-ink-gray-8">{{
												dExpanded[row.name].n_rows
											}}</span>
										</div>
										<div>
											<span class="text-ink-gray-5">Wilson low</span>
											<span class="ml-1.5 text-ink-gray-8">{{
												pct(mul100(dExpanded[row.name].wilson_low))
											}}</span>
										</div>
										<div>
											<span class="text-ink-gray-5">Gap vs base</span>
											<span class="ml-1.5 text-ink-gray-8">{{
												pct(mul100(dExpanded[row.name].gap))
											}}</span>
										</div>
										<div>
											<span class="text-ink-gray-5">Exceptions</span>
											<span class="ml-1.5 text-ink-gray-8">{{
												dExpanded[row.name].exception_n
											}}</span>
										</div>
										<div v-if="dExpanded[row.name].last_validated_at">
											<span class="text-ink-gray-5">Last validated</span>
											<Tooltip
												:text="
													exactDate(
														dExpanded[row.name].last_validated_at
													)
												"
											>
												<span class="ml-1.5 text-ink-gray-8">{{
													timeAgo(dExpanded[row.name].last_validated_at)
												}}</span>
											</Tooltip>
										</div>
									</div>

									<div
										v-if="(dExpanded[row.name].roles || []).length"
										class="mt-3 flex flex-wrap items-center gap-1.5"
									>
										<span class="text-sm text-ink-gray-5">Roles:</span>
										<Badge
											v-for="r in dExpanded[row.name].roles"
											:key="r"
											variant="outline"
											theme="gray"
											:label="r"
										/>
									</div>

									<div v-if="dExpanded[row.name].compiled_preview" class="mt-3">
										<div class="mb-1 text-sm text-ink-gray-5">
											{{
												row.effective_sensitivity === "A"
													? "Compiled rule"
													: "Suggested instruction"
											}}
										</div>
										<pre
											class="overflow-x-auto whitespace-pre-wrap rounded border bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-8"
											>{{ dExpanded[row.name].compiled_preview }}</pre
										>
									</div>

									<div
										v-if="(dExpanded[row.name].exceptions || []).length"
										class="mt-3"
									>
										<div class="mb-1 text-sm text-ink-gray-5">
											Known exceptions
										</div>
										<div class="flex flex-wrap gap-1.5">
											<a
												v-for="(ex, i) in dExpanded[row.name].exceptions"
												:key="i"
												:href="exDeskUrl(ex)"
												:target="exDeskUrl(ex) ? '_blank' : undefined"
												rel="noopener"
												class="inline-flex items-center gap-1 rounded bg-surface-gray-2 px-2 py-0.5 text-sm text-ink-gray-8"
												:class="
													exDeskUrl(ex)
														? 'hover:bg-surface-gray-3'
														: 'cursor-default'
												"
											>
												{{ exLabel(ex) }}
												<FeatherIcon
													v-if="exDeskUrl(ex)"
													name="external-link"
													class="size-3 text-ink-gray-5"
												/>
											</a>
										</div>
									</div>

									<div class="mt-3 flex flex-wrap items-center gap-3 text-sm">
										<a
											v-if="dExpanded[row.name].last_seen_run"
											:href="
												deskUrl(
													'Jarvis Pattern Run',
													dExpanded[row.name].last_seen_run
												)
											"
											target="_blank"
											rel="noopener"
											class="inline-flex items-center gap-1 text-ink-gray-7 hover:underline"
										>
											<FeatherIcon name="activity" class="size-3.5" />
											Run {{ dExpanded[row.name].last_seen_run }}
										</a>
										<a
											v-if="dExpanded[row.name].materialized_skill"
											:href="
												deskUrl(
													'Jarvis Custom Skill',
													dExpanded[row.name].materialized_skill
												)
											"
											target="_blank"
											rel="noopener"
											class="inline-flex items-center gap-1 text-ink-gray-7 hover:underline"
										>
											<FeatherIcon name="zap" class="size-3.5" />
											{{ dExpanded[row.name].materialized_skill }}
										</a>
										<span class="text-ink-gray-4">{{ row.name }}</span>
									</div>
								</template>
							</div>
						</div>
					</div>

					<!-- N of M + load more -->
					<div v-if="decided.rows.length" class="flex flex-col items-center gap-2 pt-1">
						<span class="text-sm text-ink-gray-5">
							{{ decided.rows.length }} of {{ decided.total }}
						</span>
						<Button
							v-if="decided.hasMore"
							variant="subtle"
							label="Load more"
							:loading="decided.loading"
							@click="fetchDecided('more')"
						/>
					</div>
				</section>
			</div>
		</div>

		<!-- Reject reason modal -->
		<Dialog v-model="rejectDialog.show" :options="{ title: 'Reject pattern', size: 'md' }">
			<template #body-content>
				<p class="text-sm text-ink-gray-6">
					Rejected patterns are hidden but reversible - you can restore them from the
					Rejected filter later.
				</p>
				<FormControl
					type="textarea"
					class="mt-3"
					label="Reason"
					placeholder="Why is this not a habit worth teaching?"
					:modelValue="rejectDialog.reason"
					@update:modelValue="(v) => (rejectDialog.reason = v)"
				/>
			</template>
			<template #actions>
				<div class="flex items-center gap-2">
					<Button
						variant="solid"
						theme="red"
						label="Reject"
						:loading="acting === rejectDialog.name"
						@click="submitReject"
					/>
					<Button label="Cancel" @click="rejectDialog.show = false" />
				</div>
			</template>
		</Dialog>

		<!-- Edit-then-approve modal -->
		<Dialog v-model="editDialog.show" :options="{ title: 'Edit and approve', size: 'lg' }">
			<template #body-content>
				<p class="text-sm text-ink-gray-6">
					Your edit is used verbatim in the compiled skill. The evidence line stays
					frozen - it still reflects the originally detected pattern, which was not
					re-measured.
				</p>
				<FormControl
					type="textarea"
					class="mt-3"
					label="Skill rule"
					:modelValue="editDialog.draft"
					:rows="8"
					@update:modelValue="(v) => (editDialog.draft = v)"
				/>
			</template>
			<template #actions>
				<div class="flex items-center gap-2">
					<Button
						variant="solid"
						theme="green"
						label="Approve with edits"
						:loading="acting === editDialog.name"
						@click="submitEdit"
					/>
					<Button label="Cancel" @click="editDialog.show = false" />
				</div>
			</template>
		</Dialog>

		<!-- Apply confirm modal: the restart warning is mandatory before Apply
		     proceeds; shows a live active-chat count when available and points to
		     tonight's quiet window as an alternative. -->
		<Dialog
			v-model="applyDialog.show"
			:options="{ title: 'Apply learned skills?', size: 'md' }"
		>
			<template #body-content>
				<div class="flex flex-col gap-3 text-sm">
					<div
						class="flex items-start gap-2 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-ink-amber-3"
					>
						<FeatherIcon name="alert-triangle" class="mt-0.5 size-4 shrink-0" />
						<span>
							Applying restarts the assistant for everyone for up to ~3 min. Active
							chats may be briefly interrupted.
						</span>
					</div>
					<p class="text-ink-gray-6">
						Recompiles approved patterns into the learned-&lt;domain&gt; skills and
						pushes them to your assistant. Only A-class (aggregate-safe) patterns are
						compiled; B and C insights are never pushed.
					</p>
					<p v-if="activeChatCount != null" class="text-ink-gray-7">
						{{ activeChatCount }} chat{{ activeChatCount === 1 ? "" : "s" }} active
						right now.
					</p>
					<p v-if="status.nextRunAt" class="text-ink-gray-5">
						Prefer off-hours? Cancel and apply during a quiet period (next analysis
						window
						{{ timeAgo(status.nextRunAt) }}).
					</p>
				</div>
			</template>
			<template #actions>
				<div class="flex items-center gap-2">
					<Button
						variant="solid"
						label="Apply now"
						iconLeft="upload-cloud"
						:loading="applyActive"
						@click="confirmApply"
					/>
					<Button label="Cancel" @click="applyDialog.show = false" />
				</div>
			</template>
		</Dialog>

		<!-- Apply-insight-to-skill modal (D5): self-contained - drafts an LLM
		     skill update for a B/C insight and applies it on confirm (the server
		     marks the pattern acknowledged with an applied-to-skill note); the
		     board only opens it and refreshes on completion. -->
		<InsightApplyDialog
			v-model="insightApplyDialog.show"
			:pattern="insightApplyDialog.row || {}"
			@applied="afterAction"
		/>

		<!-- "Ask the user" follow-up modal (Skills-area rework): the ask is
		     delivered generically - the user sees an ordinary org question, never
		     the reviewer's name. Shared by pattern + promotion cards. -->
		<Dialog v-model="askDialog.show" :options="{ title: 'Ask the user', size: 'md' }">
			<template #body-content>
				<p class="text-sm text-ink-gray-6">
					The user will see this as an ordinary question from your organisation — no
					reviewer name. It lands in their Personalise questions.
				</p>
				<FormControl
					type="textarea"
					class="mt-3"
					label="What would you like to ask?"
					:rows="4"
					placeholder="e.g. Should this also apply to rush orders?"
					:modelValue="askDialog.ask"
					@update:modelValue="(v) => (askDialog.ask = v)"
				/>
			</template>
			<template #actions>
				<div class="flex items-center gap-2">
					<Button
						variant="solid"
						label="Send"
						:loading="askDialog.sending"
						@click="submitAsk"
					/>
					<Button
						label="Cancel"
						:disabled="askDialog.sending"
						@click="askDialog.show = false"
					/>
				</div>
			</template>
		</Dialog>

		<!-- Promotion reject modal (Skills-area rework): mirrors the pattern reject
		     modal. The requester's personal page stays intact; the reason (optional)
		     becomes the decision note they can act on. -->
		<Dialog v-model="promoReject.show" :options="{ title: 'Reject promotion', size: 'md' }">
			<template #body-content>
				<p class="text-sm text-ink-gray-6">
					The requester's personal page stays intact. Let them know why this won't be
					published — they can revise and request again.
				</p>
				<FormControl
					type="textarea"
					class="mt-3"
					label="Reason (optional)"
					placeholder="Why not publish this to the wider audience?"
					:modelValue="promoReject.reason"
					@update:modelValue="(v) => (promoReject.reason = v)"
				/>
			</template>
			<template #actions>
				<div class="flex items-center gap-2">
					<Button
						variant="solid"
						theme="red"
						label="Reject"
						:loading="promoActing === (promoReject.p && promoReject.p.name)"
						@click="submitPromoReject"
					/>
					<Button label="Cancel" @click="promoReject.show = false" />
				</div>
			</template>
		</Dialog>

		<!-- Skill-promotion reject modal (Skills-area promotion surfacing):
		     mirrors the wiki promotion reject modal. The requester's private
		     skill stays intact; the reason (optional) becomes the decision note. -->
		<Dialog
			v-model="skillPromoReject.show"
			:options="{ title: 'Reject skill promotion', size: 'md' }"
		>
			<template #body-content>
				<p class="text-sm text-ink-gray-6">
					The requester's private skill stays intact. Let them know why this won't be
					widened — they can revise and request again.
				</p>
				<FormControl
					type="textarea"
					class="mt-3"
					label="Reason (optional)"
					placeholder="Why not share this more widely?"
					:modelValue="skillPromoReject.reason"
					@update:modelValue="(v) => (skillPromoReject.reason = v)"
				/>
			</template>
			<template #actions>
				<div class="flex items-center gap-2">
					<Button
						variant="solid"
						theme="red"
						label="Reject"
						:loading="
							skillPromoActing === (skillPromoReject.p && skillPromoReject.p.name)
						"
						@click="submitSkillPromoReject"
					/>
					<Button label="Cancel" @click="skillPromoReject.show = false" />
				</div>
			</template>
		</Dialog>
	</div>
</template>

<script setup>
// ReviewTab - the pattern decision queue, the "Review" tab inside the Skills
// page (Skills IA v2). Two panes at xl+ (stacked below): LEFT "To review" owns
// the Apply bar (learned skills ride the shared custom-skill push - the
// SyncPill pattern), a debounced search + status select, domain facet chips,
// the review board of plain-English cards with strength/sensitivity chips,
// caveat badges, an expandable drill-down (raw stats + roles + compiled bullet
// + Desk links via get_learned_pattern), the Approve / Edit-then-approve /
// Reject / Snooze / Un-approve / Restore / batch-approve actions, and queued
// (not-yet-surfaced) proposals inline under a divider with full actions; the
// pane header says "N to review · M queued" because the tab badge counts
// surfaced rows only. RIGHT "Decided" is a compact log (view="decided"):
// one-line rows (excerpt + disposition badge + who/when) with its own search /
// disposition / newest-oldest sort and click-to-expand drill-down. Each
// actionable row gets "Discuss in chat" - a prefilled prompt stashed via
// chatPrefill with autoSend, so ChatView opens a FRESH conversation and sends
// it as the first message. Both fetchers use the useListPage monotonic-reqId
// guard so reset/load-more/search races never interleave. Settings + run
// telemetry live in AnalysisTab. Managed-only: get_learning_status reports
// self_hosted → this renders the managed-only empty state.
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from "vue";
import {
	Avatar,
	Badge,
	Breadcrumbs,
	Button,
	Checkbox,
	Dialog,
	Dropdown,
	FeatherIcon,
	FormControl,
	LoadingIndicator,
	Tooltip,
	confirmDialog,
	toast,
} from "frappe-ui";
import { useRouter } from "vue-router";
import LayoutHeader from "@/components/LayoutHeader.vue";
import SyncPill from "./SyncPill.vue";
import InsightApplyDialog from "@/components/learning/InsightApplyDialog.vue";
import { timeAgo, exactDate } from "@/utils/datetime";
import { setChatPrefill } from "@/composables/chatPrefill";
import { getSkillsAreaCaps } from "@/api/personalise";
import {
	listLearnedPatternsPage,
	getLearnedPattern,
	approveLearnedPattern,
	unapproveLearnedPattern,
	rejectLearnedPattern,
	acknowledgeLearnedPattern,
	restoreRejectedPattern,
	snoozeLearnedPattern,
	batchApprove,
	applyLearnedSkills,
	getLearnedApplyStatus,
	getLearningStatus,
} from "@/api/learning";
// Skills-area rework additions (DESIGN.md §6b): the reviewer-only bindings live
// in their own module so this wave never edits F1's learning.js. getReviewAccess
// is the tab's own probe (reviewer set, decoupled from the Analysis probe);
// the rest power the Promotions queue, the server "Go to chat" bundle and the
// "Ask the user" follow-up.
import {
	getReviewAccess,
	listPromotionRequestsPage,
	decidePromotion,
	goToChatContext,
	triggerFollowupQuestion,
	listSkillPromotions,
	decideSkillPromotion,
} from "@/api/review";
// Org push-budget warning boundary (ruling 2) — a pure, node-tested module.
import { orgPushBudgetWarning } from "./promotionBudget";

const emit = defineEmits(["changed"]);
const router = useRouter();

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

// ── static config ────────────────────────────────────────────────────────────
const STATUS_OPTIONS = [
	{ label: "To review", value: "Proposed" },
	{ label: "Approved", value: "Approved" },
	{ label: "Active", value: "Active" },
	{ label: "Snoozed", value: "Snoozed" },
	{ label: "Stale", value: "Stale" },
	{ label: "Rejected", value: "Rejected" },
	{ label: "All", value: "All" },
];
// Decided-log disposition filter - server-validated slugs (decided view only).
const DISPOSITION_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Approved", value: "approved" },
	{ label: "Applied to skill", value: "applied" },
	{ label: "Acknowledged", value: "acknowledged" },
	{ label: "Rejected", value: "rejected" },
	{ label: "Snoozed", value: "snoozed" },
];
const STATUS_THEME = {
	Proposed: "blue",
	Approved: "green",
	Active: "green",
	Rejected: "red",
	Snoozed: "gray",
	Stale: "orange",
	Superseded: "gray",
	Archived: "gray",
};
const DOMAIN_LABELS = {
	selling: "Selling",
	buying: "Buying",
	stock: "Stock",
	accounts: "Accounts",
	projects: "Projects",
	org: "Org",
};
const SNOOZE_DAYS = [7, 30, 90];
// Mirror learned_api.ACK_NOTE: the review_note a B/C Acknowledge stamps. Keep in
// sync with the server constant; used to render an acknowledged (insight-only)
// row distinctly from a real rejection. Needs the card to expose review_note.
const ACK_NOTE = "Acknowledged - insight only";

// ── state ────────────────────────────────────────────────────────────────────
const selfHosted = ref(false);
const acting = ref(""); // pattern name (or "__batch__") currently acting

// slim status slice (the full telemetry lives on the Analysis tab): enabled
// feeds the empty-state copy, nextRunAt the apply dialog's quiet-window hint.
const status = reactive({
	enabled: false,
	nextRunAt: "",
});

const board = reactive({ rows: [], total: 0, hasMore: false, loading: false });
const boardSearch = ref("");
const facets = ref([]);
const queuedCount = ref(0);
const pendingApplyCount = ref(0);
// Stale rows still compiled into a live skill: removed on the next Apply (§6.5).
const staleRemovalCount = ref(0);
const reviewActivity = ref({ decided: 0, total: 0, last_by_name: "" });
const domain = ref("");
const boardStatus = ref("Proposed");

// Decided log pane: fetched on mount alongside the board; afterAction refreshes
// it so fresh decisions land without a manual reload.
const decided = reactive({
	loading: false,
	rows: [],
	total: 0,
	hasMore: false,
	search: "",
	disposition: "",
	sort: "newest",
});

const expanded = reactive({}); // board drill-down: name -> detail | "loading"
// Decided drill-down cache - separate from `expanded` so a pattern visible in
// both panes at once never couples its open/close state across surfaces.
const dExpanded = reactive({}); // name -> detail | "loading"
const selected = ref(new Set());
const selectedNames = computed(() => Array.from(selected.value));
const syncPill = ref(null);

// apply lifecycle: applyActive keeps the Apply bar (and its SyncPill) mounted
// through the apply->poll->done cycle even after pendingApplyCount drops to 0,
// so the push progress / failure never vanishes mid-flight (mirrors the Skills
// list banner, which is always mounted).
const applyActive = ref(false);
let applyTimer = null;
// A cheap "active chats right now" count would render in the Apply modal; no
// endpoint exposes it yet, so it stays null and the line is omitted gracefully.
const activeChatCount = ref(null);

// dialogs
const rejectDialog = reactive({ show: false, name: "", reason: "" });
const editDialog = reactive({ show: false, name: "", draft: "" });
const applyDialog = reactive({ show: false });
const insightApplyDialog = reactive({ show: false, row: null });

// ── Skills-area rework state ──────────────────────────────────────────────────
// LEFT-pane queue-type chips (additive; the accepted two-pane structure is
// unchanged). "candidates" = the existing skill-candidate board; "promotions" =
// the new wiki-promotion queue rendered with the same card rhythm.
const queueType = ref("candidates");
// Promotion queue: its own reqId-guarded envelope + Pending count for the chip.
// Seeded from the get_review_access probe so the chip reads before the list is
// fetched (lazily, the first time the Promotions chip is opened).
const promo = reactive({ rows: [], total: 0, hasMore: false, loading: false });
const promoLoaded = ref(false);
const promoActing = ref(""); // promotion request name currently deciding
const promoExpanded = reactive({}); // name -> bool (un-clamp the body excerpt)
const promoReject = reactive({ show: false, p: null, reason: "" });
// Skill-promotion queue (sibling of the wiki promotions queue; the reviewer side
// skills never had). Its own reqId-guarded envelope + Pending count for the chip,
// plus the container push-budget context (pushCount/pushBudget) that drives the
// ruling-2 Org-approval warning. Fetched lazily the first time its chip opens.
const skillPromo = reactive({
	rows: [],
	total: 0,
	hasMore: false,
	loading: false,
	pushCount: 0,
	pushBudget: 0,
});
const skillPromoLoaded = ref(false);
const skillPromoActing = ref(""); // skill promotion request name currently deciding
const skillPromoExpanded = reactive({}); // name -> bool (un-clamp the instructions excerpt)
const skillPromoReject = reactive({ show: false, p: null, reason: "" });
// "Ask the user" follow-up dialog, shared by pattern + promotion cards. `name`
// is the review-item name (a Jarvis Learned Pattern or a promotion request).
const askDialog = reactive({ show: false, name: "", ask: "", sending: false });

// ── display helpers ──────────────────────────────────────────────────────────
function domainLabel(s) {
	return DOMAIN_LABELS[s] || (s ? s[0].toUpperCase() + s.slice(1) : s);
}
function strengthTheme(b) {
	return b === "High" ? "green" : b === "Medium" ? "orange" : "gray";
}
// Sensitivity badge. B is two different things and must not both read as
// "Party-specific": a row DECLARED A but escalated to B by content (a specific
// customer/supplier is named) is genuinely party-specific; a detector that
// DECLARES B (process-control shortcuts like the update_stock bypass or rounding
// disable: buy-pi-update-stock, acct-rounding-habit) is org-level
// operational friction, so it reads as "Sensitive". We infer the split from
// declared vs effective; a dedicated sensitivity_kind field on the card would be
// more robust (see the API note).
function sensBadge(row) {
	const eff = row.effective_sensitivity || "";
	if (eff === "C") return { theme: "red", label: "Financial (insight only)" };
	if (eff === "B") {
		return (row.sensitivity || "") === "A"
			? { theme: "orange", label: "Party-specific" }
			: { theme: "orange", label: "Sensitive" };
	}
	return { theme: "gray", label: "Aggregate-safe" };
}
// The exact-text B-class disclosure banner (plan section 6.6): learned skill
// files are bench-global, so Allowed Roles steers activation but is not a
// confidentiality boundary. Names the detected roles when the drill-down has
// loaded them; otherwise a generic phrasing.
function disclosureText(roles) {
	const named = roles && roles.length ? roles.join(", ") : "its detected roles";
	return (
		"This text becomes readable by every chat user on this site, regardless of ERP " +
		`permissions; scoped to ${named} in normal operation but not hidden from a user who looks.`
	);
}
function bRoles(row) {
	const d = expanded[row.name];
	return d && d !== "loading" && Array.isArray(d.roles) ? d.roles : [];
}
// An acknowledged (insight-only) row is stored as a terminal Rejected + ACK_NOTE;
// render it as "Acknowledged", not a rejection. Requires review_note on the card
// payload (see the API note); falls back to plain Rejected until then.
function isAcknowledged(row) {
	return (row.review_note || "") === ACK_NOTE;
}
// Mirror learned_api.APPLIED_NOTE_PREFIX: an insight folded into a custom
// skill is terminal-Rejected + this note; render the target, not "Rejected".
const APPLIED_NOTE_PREFIX = "Acknowledged - applied to skill: ";
function appliedSkill(row) {
	const note = row.review_note || "";
	return note.startsWith(APPLIED_NOTE_PREFIX) ? note.slice(APPLIED_NOTE_PREFIX.length) : "";
}
// Decided-log disposition: the human outcome, not the raw status. Applied-to-
// skill and Acknowledged are stored as terminal Rejected + note, so they must
// win over the status fallback. Compact labels for the log rows; `skill`
// carries the applied-to target for the badge tooltip.
function dispositionBadge(row) {
	const skill = appliedSkill(row);
	if (skill) return { theme: "green", label: "Applied", skill };
	if (isAcknowledged(row)) return { theme: "gray", label: "Acknowledged" };
	if (row.status === "Rejected") return { theme: "red", label: "Rejected" };
	if (row.status === "Snoozed") return { theme: "orange", label: "Snoozed" };
	if (row.status === "Approved" || row.status === "Active")
		return { theme: "green", label: "Approved" };
	return { theme: STATUS_THEME[row.status] || "gray", label: row.status };
}
function excerpt(s, n = 220) {
	const t = String(s || "").trim();
	return t.length > n ? t.slice(0, n - 1).trimEnd() + "…" : t;
}
function pct(v) {
	const n = Number(v || 0);
	return `${Math.round(n * 10) / 10}%`;
}
function mul100(v) {
	return Number(v || 0) * 100;
}
function deskUrl(doctype, name) {
	if (!doctype || !name) return "";
	const dt = String(doctype).toLowerCase().replace(/ /g, "-");
	return `/app/${dt}/${encodeURIComponent(name)}`;
}
// exceptions items may be strings or {doctype,name,label,...} - render defensively
function exLabel(ex) {
	if (ex == null) return "";
	if (typeof ex === "string") return ex;
	return ex.label || ex.name || ex.value || JSON.stringify(ex);
}
function exDeskUrl(ex) {
	if (ex && typeof ex === "object" && ex.doctype && ex.name) return deskUrl(ex.doctype, ex.name);
	return "";
}

// The board fetches surfaced=all on the default queue view and partitions
// client-side: surfaced rows first, queued rows under the divider (one fetch,
// full actions on both). Other status views render as a single group.
const rowGroups = computed(() => {
	if (boardStatus.value !== "Proposed") return [{ key: "main", rows: board.rows }];
	const groups = [{ key: "main", rows: board.rows.filter((r) => Number(r.surfaced)) }];
	const queued = board.rows.filter((r) => !Number(r.surfaced));
	if (queued.length) groups.push({ key: "queued", rows: queued });
	return groups;
});
const queuedVisible = computed(
	() => boardStatus.value === "Proposed" && board.rows.some((r) => !Number(r.surfaced))
);
const hasActionableB = computed(() =>
	board.rows.some(
		(r) =>
			r.effective_sensitivity === "B" && ["Proposed", "Stale", "Snoozed"].includes(r.status)
	)
);

// The pane-header "N to review": surfaced Proposed = review_activity.total
// (surfaced ∈ Proposed/Approved/Rejected/Snoozed) minus .decided (the latter
// three) - the same number the tab badge shows, kept honest next to the
// site-wide queued count.
const toReviewCount = computed(() =>
	Math.max((reviewActivity.value.total || 0) - (reviewActivity.value.decided || 0), 0)
);

const decidedFiltered = computed(() => !!(decided.search.trim() || decided.disposition));

const emptyTitle = computed(() => {
	if (boardSearch.value.trim()) return "No matching patterns";
	return boardStatus.value === "Proposed" ? "Nothing to review yet" : "No patterns here";
});
const emptyDescription = computed(() => {
	if (boardSearch.value.trim())
		return "Nothing matches your search - try different words or clear it.";
	if (boardStatus.value === "Proposed") {
		if (queuedCount.value > 0)
			return `Nothing under this filter - ${queuedCount.value} queued site-wide. Queued proposals appear in this list; try clearing the domain filter.`;
		return status.enabled
			? "Proposals appear the morning after an analysis run."
			: "Enable behavioural learning on the Analysis tab, then run an analysis to see proposals.";
	}
	return "Try a different domain or status filter.";
});

// ── loaders ──────────────────────────────────────────────────────────────────
// Review visibility is DECOUPLED from Analysis now (DESIGN.md §6): the tab's own
// probe is get_review_access (reviewer set, self-host aware), NOT
// get_learning_status (which is admin-gated and 403s a plain reviewer). It also
// seeds the Promotions chip count so the queue reads before its list loads.
async function loadStatus() {
	try {
		const st = await getReviewAccess();
		selfHosted.value = !!st.self_hosted;
		promo.total = st.pending_promotions || 0;
		skillPromo.total = st.pending_skill_promotions || 0;
	} catch (e) {
		// parent mounts this only for the reviewer set; a failure here = no access
		selfHosted.value = false;
		toast.error(errMsg(e));
	}
	// Best-effort Analysis niceties for reviewers who ALSO hold the admin role:
	// the empty-state "enable learning" copy + the Apply dialog's quiet-window
	// hint. Gate on the caps probe first so a plain reviewer never fires the
	// admin-only endpoint at all (a deliberate 403 still logs as a console /
	// network error - QA flagged the noise).
	try {
		const c = await getSkillsAreaCaps();
		if (!c || !c.analysis) return;
		const ls = await getLearningStatus();
		status.enabled = !!ls.enabled;
		status.nextRunAt = ls.next_run_at || "";
	} catch (e) {
		// reviewer without Analysis access - degrade quietly
	}
}

// ── promotions queue (Skills-area rework) ────────────────────────────────────
// Own monotonic reqId guard (the board/decided idiom): a reset racing a
// load-more never interleaves. Fetched lazily the first time the Promotions chip
// is opened; refreshed after every decide.
let promoReq = 0;
async function fetchPromotions(mode = "reset") {
	const id = ++promoReq;
	const append = mode === "more";
	promo.loading = true;
	try {
		const res = await listPromotionRequestsPage({
			status: "Pending",
			start: append ? promo.rows.length : 0,
			page_length: 20,
		});
		if (id !== promoReq) return; // stale - a newer request superseded this one
		promo.rows = mergeRows(promo.rows, res.rows || [], append);
		promo.total = res.total || 0;
		promo.hasMore = !!res.has_more;
		promoLoaded.value = true;
	} catch (e) {
		if (id !== promoReq) return;
		toast.error(errMsg(e));
	} finally {
		if (id === promoReq) promo.loading = false;
	}
}
function setQueueType(v) {
	if (queueType.value === v) return;
	queueType.value = v;
	if (v === "promotions" && !promoLoaded.value) fetchPromotions("reset");
	if (v === "skillpromotions" && !skillPromoLoaded.value) fetchSkillPromotions("reset");
}
function togglePromoBody(name) {
	promoExpanded[name] = !promoExpanded[name];
}
function toScopeLabel(p) {
	return p.to_scope === "Role" ? `Role: ${p.target_role || "—"}` : p.to_scope || "Org";
}
// The decision is irreversible from the user's side, so Approve confirms with
// the concrete visibility implication (who can now read the page).
function approvePromotion(p) {
	const target = p.to_scope === "Role" ? `Role: ${p.target_role || "—"}` : "Org";
	const who = p.to_scope === "Role" ? "that role" : "everyone";
	confirmDialog({
		title: "Approve promotion?",
		message:
			`This publishes “${p.page_title}” to ${target} — visible to ${who}. ` +
			"The requester's personal page stays intact.",
		onConfirm: async ({ hideDialog }) => {
			hideDialog();
			await decidePromo(p, 1, "");
		},
	});
}
function openPromoReject(p) {
	promoReject.p = p;
	promoReject.reason = "";
	promoReject.show = true;
}
async function submitPromoReject() {
	if (!promoReject.p) return;
	const ok = await decidePromo(promoReject.p, 0, (promoReject.reason || "").trim());
	if (ok) promoReject.show = false;
}
async function decidePromo(p, approve, note) {
	promoActing.value = p.name;
	try {
		const r = await decidePromotion(p.name, approve, note);
		if (r && r.ok === false) {
			toast.error(r.reason || "Could not decide this promotion.");
			return false;
		}
		toast.success(approve ? "Promotion approved" : "Promotion rejected");
		fetchPromotions("reset");
		emit("changed");
		return true;
	} catch (e) {
		toast.error(errMsg(e));
		return false;
	} finally {
		promoActing.value = "";
	}
}

// ── skill-promotions queue (Skills-area promotion surfacing) ─────────────────
// The sibling of the wiki promotions queue, its own reqId guard. Envelope also
// carries push_count/push_budget so the Org approve affordance can warn near the
// container cap (ruling 2). Fetched lazily on first open; refreshed after decide.
let skillPromoReq = 0;
async function fetchSkillPromotions(mode = "reset") {
	const id = ++skillPromoReq;
	const append = mode === "more";
	skillPromo.loading = true;
	try {
		const res = await listSkillPromotions({
			status: "Pending",
			start: append ? skillPromo.rows.length : 0,
			page_length: 20,
		});
		if (id !== skillPromoReq) return; // stale - a newer request superseded this one
		skillPromo.rows = mergeRows(skillPromo.rows, res.rows || [], append);
		skillPromo.total = res.total || 0;
		skillPromo.hasMore = !!res.has_more;
		skillPromo.pushCount = res.push_count || 0;
		skillPromo.pushBudget = res.push_budget || 0;
		skillPromoLoaded.value = true;
	} catch (e) {
		if (id !== skillPromoReq) return;
		toast.error(errMsg(e));
	} finally {
		if (id === skillPromoReq) skillPromo.loading = false;
	}
}
function toggleSkillPromoBody(name) {
	skillPromoExpanded[name] = !skillPromoExpanded[name];
}
// Loud, non-blocking Org push-budget warning (ruling 2): a promoted Org skill
// only reaches the container on the next Apply, and the push is capped at
// push_budget. This returns null (no warning) for Role targets or when there's
// room; the approval is ALWAYS allowed - the reviewer decides informed.
function budgetWarn(p) {
	return orgPushBudgetWarning({
		toScope: p.to_scope,
		pushCount: skillPromo.pushCount,
		pushBudget: skillPromo.pushBudget,
	});
}
// Approve confirms with the concrete visibility implication + the budget warning
// inline (the card already shows the loud banner; this repeats it at the point of
// action). The widen is irreversible from the requester's side.
function approveSkillPromotion(p) {
	const target = p.to_scope === "Role" ? `Role: ${p.target_role || "—"}` : "Org";
	const who = p.to_scope === "Role" ? "that role" : "everyone";
	const warn = budgetWarn(p);
	let message =
		`This widens “${p.skill_name}” to ${target} — usable by ${who}. ` +
		"The requester's private skill stays intact.";
	if (warn) message += ` ⚠ ${warn.message}`;
	confirmDialog({
		title: "Approve skill promotion?",
		message,
		onConfirm: async ({ hideDialog }) => {
			hideDialog();
			await decideSkillPromo(p, 1, "");
		},
	});
}
function openSkillPromoReject(p) {
	skillPromoReject.p = p;
	skillPromoReject.reason = "";
	skillPromoReject.show = true;
}
async function submitSkillPromoReject() {
	if (!skillPromoReject.p) return;
	const ok = await decideSkillPromo(
		skillPromoReject.p,
		0,
		(skillPromoReject.reason || "").trim()
	);
	if (ok) skillPromoReject.show = false;
}
async function decideSkillPromo(p, approve, note) {
	skillPromoActing.value = p.name;
	try {
		const r = await decideSkillPromotion(p.name, approve, note);
		if (r && r.ok === false) {
			// stale / already-decided request (TOCTOU-safe server re-read)
			toast.error(r.reason || "Could not decide this promotion.");
			return false;
		}
		toast.success(approve ? "Skill promotion approved" : "Skill promotion rejected");
		fetchSkillPromotions("reset");
		emit("changed");
		return true;
	} catch (e) {
		// Four-eyes (a reviewer cannot approve their OWN request) + reviewer-gate
		// come back as a PermissionError - surface the honest server message.
		toast.error(errMsg(e));
		return false;
	} finally {
		skillPromoActing.value = "";
	}
}

// ── "Ask the user" follow-up (Skills-area rework) ────────────────────────────
// Opens the shared dialog for a review-item name (a pattern or a promotion). The
// server rephrases the ask into a generic-tone question inserted into that user's
// bank - no reviewer attribution ever shown to the user (DESIGN.md §6).
function openAsk(name) {
	askDialog.name = name;
	askDialog.ask = "";
	askDialog.show = true;
}
async function submitAsk() {
	const ask = (askDialog.ask || "").trim();
	if (!ask) {
		toast.error("Enter what you would like to ask the user.");
		return;
	}
	askDialog.sending = true;
	try {
		await triggerFollowupQuestion(askDialog.name, ask);
		toast.success("Added to their questions");
		askDialog.show = false;
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		askDialog.sending = false;
	}
}

// Deep-link into the skill editor (SkillDetail already edits it): the direct
// "skills updatable from Review" path for an A-class pattern whose learned skill
// is already materialized. A-class rows with no skill yet keep Edit-and-approve.
function editSkill(row) {
	if (!row.materialized_skill) return;
	router.push({ name: "SkillDetail", params: { id: row.materialized_skill } });
}

// append dedupes by name so a reset racing a load-more can never mint duplicate
// v-for keys even if a stale page slips through (belt to the reqId braces)
function mergeRows(prev, next, append) {
	if (!append) return next;
	const seen = new Set(prev.map((r) => r.name));
	return [...prev, ...next.filter((r) => !seen.has(r.name))];
}

// monotonic request ids (the useListPage idiom): a reset racing a load-more -
// or two debounced searches - must not interleave; stale responses are dropped.
let boardReq = 0;
async function fetchBoard(mode = "reset") {
	const id = ++boardReq;
	const append = mode === "more";
	if (!append) selected.value = new Set();
	board.loading = true;
	try {
		// "all" even on the queue view: queued rows render inline (partitioned
		// client-side by `surfaced`) instead of hiding behind the All filter.
		const res = await listLearnedPatternsPage({
			domain: domain.value,
			status: boardStatus.value,
			search: boardSearch.value.trim(),
			surfaced: "all",
			start: append ? board.rows.length : 0,
			page_length: 20,
		});
		if (id !== boardReq) return; // stale - a newer request superseded this one
		board.rows = mergeRows(board.rows, res.rows || [], append);
		board.total = res.total || 0;
		board.hasMore = !!res.has_more;
		facets.value = (res.facets && res.facets.domain) || [];
		queuedCount.value = res.queued_count || 0;
		pendingApplyCount.value = res.pending_apply_count || 0;
		staleRemovalCount.value = res.stale_pending_removal || 0;
		reviewActivity.value = res.review_activity || { decided: 0, total: 0, last_by_name: "" };
	} catch (e) {
		if (id !== boardReq) return;
		toast.error(errMsg(e));
	} finally {
		if (id === boardReq) board.loading = false;
	}
}

let decidedReq = 0;
async function fetchDecided(mode = "reset") {
	const id = ++decidedReq;
	const append = mode === "more";
	decided.loading = true;
	try {
		// independent of the board's chips/status: the Decided log is a site-wide
		// audit trail (server orders by reviewed_at, nulls last; disposition and
		// sort are decided-view-only params)
		const res = await listLearnedPatternsPage({
			view: "decided",
			search: decided.search.trim(),
			disposition: decided.disposition,
			sort: decided.sort,
			start: append ? decided.rows.length : 0,
			page_length: 20,
		});
		if (id !== decidedReq) return; // stale - a newer request superseded this one
		decided.rows = mergeRows(decided.rows, res.rows || [], append);
		decided.total = res.total || 0;
		decided.hasMore = !!res.has_more;
	} catch (e) {
		if (id !== decidedReq) return;
		toast.error(errMsg(e));
	} finally {
		if (id === decidedReq) decided.loading = false;
	}
}

function reloadAll() {
	loadStatus();
	fetchBoard("reset");
	fetchDecided("reset");
}

// ── filters ──────────────────────────────────────────────────────────────────
function setDomain(v) {
	if (domain.value === v) return;
	domain.value = v;
	fetchBoard("reset");
}
function setBoardStatus(v) {
	if (boardStatus.value === v) return;
	boardStatus.value = v;
	fetchBoard("reset");
}
function setDisposition(v) {
	if (decided.disposition === v) return;
	decided.disposition = v;
	fetchDecided("reset");
}
function toggleDecidedSort() {
	decided.sort = decided.sort === "oldest" ? "newest" : "oldest";
	fetchDecided("reset");
}

// debounced searches (300ms - the NotesPane / useListPage idiom)
let boardSearchTimer = null;
watch(boardSearch, () => {
	clearTimeout(boardSearchTimer);
	boardSearchTimer = setTimeout(() => fetchBoard("reset"), 300);
});
let decidedSearchTimer = null;
watch(
	() => decided.search,
	() => {
		clearTimeout(decidedSearchTimer);
		decidedSearchTimer = setTimeout(() => fetchDecided("reset"), 300);
	}
);

// ── drill-down ───────────────────────────────────────────────────────────────
async function toggleExpand(name) {
	if (expanded[name]) {
		delete expanded[name];
		return;
	}
	expanded[name] = "loading";
	try {
		expanded[name] = await getLearnedPattern(name);
	} catch (e) {
		delete expanded[name];
		toast.error(errMsg(e));
	}
}
async function toggleDecidedExpand(name) {
	if (dExpanded[name]) {
		delete dExpanded[name];
		return;
	}
	dExpanded[name] = "loading";
	try {
		dExpanded[name] = await getLearnedPattern(name);
	} catch (e) {
		delete dExpanded[name];
		toast.error(errMsg(e));
	}
}

// ── discuss in chat (Skills IA v2) ───────────────────────────────────────────
// Builds a prefilled prompt (pattern statement + evidence excerpt + suggested
// instruction + the weigh-it ask) and stashes it via the chatPrefill composable
// with autoSend: ChatView opens a FRESH conversation and sends it as the first
// message - the drafted prompt never lands in an old thread's composer. Uses
// the drill-down detail when it is already loaded; falls back to card fields
// (no extra fetch).
function buildDiscussPrompt(row, detail) {
	const parts = [];
	parts.push(
		`I'm reviewing a learned-pattern proposal for our ${domainLabel(row.domain) || "org"} ` +
			`workflow${row.company ? ` (${row.company})` : ""}.`
	);
	parts.push(`Pattern: ${excerpt(row.pattern_statement, 300)}`);
	const ev = [];
	if (detail) {
		if (detail.frozen_evidence_label) {
			ev.push(excerpt(detail.frozen_evidence_label, 200));
		} else if (detail.detector_id === "voice-context") {
			ev.push("from spoken or written business notes");
		} else {
			if (detail.confidence_pct != null) ev.push(`confidence ${pct(detail.confidence_pct)}`);
			if (detail.support_n != null) ev.push(`${detail.support_n} supporting units`);
			if (detail.n_rows != null) ev.push(`${detail.n_rows} rows scanned`);
		}
	} else {
		ev.push(`strength ${row.strength_band || "Low"}`);
	}
	if (row.exception_n)
		ev.push(`${row.exception_n} known exception${row.exception_n === 1 ? "" : "s"}`);
	if (row.flags_count)
		ev.push(
			`flagged as wrong by chat users ${row.flags_count} time${
				row.flags_count === 1 ? "" : "s"
			}`
		);
	if (ev.length) parts.push(`Evidence: ${ev.join(", ")}.`);
	const draft = (detail && detail.compiled_preview) || row.skill_draft || "";
	if (draft) parts.push(`Suggested instruction: ${excerpt(draft, 350)}`);
	parts.push(
		"Help me weigh adopting this as an org-wide default: what are the benefits, " +
			"what are the risks, and what should I check in our data before deciding?"
	);
	let text = parts.join("\n\n");
	if (text.length > 1200) text = text.slice(0, 1199).trimEnd() + "…";
	return text;
}
// "Go to chat" (Skills-area rework): prefer the server-assembled background
// bundle (go_to_chat_context - richer: origin, the linked question + the user's
// answer, who the user is + roles, the approval implication, a unified diff).
// On ANY endpoint error for a pattern, fall back SILENTLY to the client-built
// buildDiscussPrompt (no toast) so the reviewer is never blocked; promotions have
// no client fallback, so a failure there surfaces an error toast.
async function goToChat(kind, name, patternRow) {
	try {
		const res = await goToChatContext(kind, name);
		const text = (res && res.prompt) || "";
		if (!text) throw new Error("empty bundle");
		setChatPrefill({ text, autoSend: true });
		router.push("/");
	} catch (e) {
		if (kind === "pattern" && patternRow) {
			const d = expanded[patternRow.name];
			setChatPrefill({
				text: buildDiscussPrompt(patternRow, d && d !== "loading" ? d : null),
				autoSend: true,
			});
			router.push("/");
			return;
		}
		toast.error(errMsg(e));
	}
}

// ── selection / batch ────────────────────────────────────────────────────────
function toggleSelect(row) {
	if (row.effective_sensitivity !== "A") return;
	const s = new Set(selected.value);
	if (s.has(row.name)) s.delete(row.name);
	else s.add(row.name);
	selected.value = s;
}
async function doBatchApprove() {
	if (!selectedNames.value.length) return;
	acting.value = "__batch__";
	try {
		const r = await batchApprove(selectedNames.value);
		toast.success(`Approved ${r.count || selectedNames.value.length}`);
		afterAction();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		acting.value = "";
	}
}

// ── single-pattern actions ───────────────────────────────────────────────────
function afterAction() {
	fetchBoard("reset");
	fetchDecided("reset");
	emit("changed");
}

async function doApprove(row) {
	acting.value = row.name;
	try {
		await approveLearnedPattern(row.name);
		toast.success("Approved");
		afterAction();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		acting.value = "";
	}
}
async function doUnapprove(row) {
	acting.value = row.name;
	try {
		await unapproveLearnedPattern(row.name);
		toast.success("Un-approved");
		afterAction();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		acting.value = "";
	}
}
async function doAcknowledge(row) {
	acting.value = row.name;
	try {
		await acknowledgeLearnedPattern(row.name);
		toast.success("Acknowledged");
		afterAction();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		acting.value = "";
	}
}
async function doRestore(row) {
	acting.value = row.name;
	try {
		await restoreRejectedPattern(row.name);
		toast.success("Restored");
		afterAction();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		acting.value = "";
	}
}
function snoozeOptions(row) {
	return SNOOZE_DAYS.map((d) => ({ label: `${d} days`, onClick: () => doSnooze(row, d) }));
}
async function doSnooze(row, days) {
	acting.value = row.name;
	try {
		await snoozeLearnedPattern(row.name, days);
		toast.success(`Snoozed ${days} days`);
		afterAction();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		acting.value = "";
	}
}

// reject modal
function openReject(row) {
	rejectDialog.name = row.name;
	rejectDialog.reason = "";
	rejectDialog.show = true;
}
async function submitReject() {
	const reason = (rejectDialog.reason || "").trim();
	if (!reason) {
		toast.error("A rejection reason is required.");
		return;
	}
	acting.value = rejectDialog.name;
	try {
		await rejectLearnedPattern(rejectDialog.name, reason);
		toast.success("Rejected");
		rejectDialog.show = false;
		afterAction();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		acting.value = "";
	}
}

// insight → skill modal (D5): the dialog drafts, confirms and applies on its
// own (incl. the "Acknowledge instead" shortcut); @applied → afterAction so
// the board refetches and the parent badge updates.
function openInsightApply(row) {
	insightApplyDialog.row = row;
	insightApplyDialog.show = true;
}

// edit-then-approve modal
function openEdit(row) {
	editDialog.name = row.name;
	editDialog.draft = row.skill_draft || "";
	editDialog.show = true;
}
async function submitEdit() {
	acting.value = editDialog.name;
	try {
		await approveLearnedPattern(editDialog.name, editDialog.draft);
		toast.success("Approved with edits");
		editDialog.show = false;
		afterAction();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		acting.value = "";
	}
}

// Apply opens a confirm modal (restart warning is mandatory before proceeding);
// confirmApply does the push and keeps progress mounted through the poll.
function applyLearned() {
	applyDialog.show = true;
}

async function confirmApply() {
	try {
		await applyLearnedSkills();
		applyDialog.show = false;
		applyActive.value = true;
		toast.success("Applying learned skills…");
		// learned skills ride the shared custom-skill push; the SyncPill polls
		// the live status; we also poll to keep the bar mounted and surface the
		// final success/failure.
		syncPill.value && syncPill.value.checkNow();
		startApplyPoll();
		fetchBoard("reset");
		emit("changed");
	} catch (e) {
		toast.error(errMsg(e));
	}
}

function stopApplyPoll() {
	if (applyTimer) {
		clearInterval(applyTimer);
		applyTimer = null;
	}
}
function startApplyPoll() {
	stopApplyPoll();
	applyTimer = setInterval(async () => {
		let st;
		try {
			st = await getLearnedApplyStatus();
		} catch (e) {
			return; // transient; keep the bar up and keep polling
		}
		if (st && st.pending) return;
		stopApplyPoll();
		applyActive.value = false;
		const s = (st && st.last_sync_status) || "";
		if (s.startsWith("failed")) {
			toast.error(s.replace(/^failed:?/, "").trim() || "Applying learned skills failed.");
		} else {
			toast.success("Learned skills applied to your assistant.");
		}
		fetchBoard("reset");
		emit("changed");
	}, 3000);
}

// ── init ─────────────────────────────────────────────────────────────────────
onMounted(async () => {
	await loadStatus();
	if (selfHosted.value) return;
	fetchBoard("reset");
	fetchDecided("reset");
	// resume progress if an Apply is already in flight (page reloaded mid-push)
	try {
		const st = await getLearnedApplyStatus();
		if (st && st.pending) {
			applyActive.value = true;
			syncPill.value && syncPill.value.checkNow();
			startApplyPoll();
		}
	} catch (e) {
		// best-effort resume; never block the tab
	}
});
onBeforeUnmount(() => {
	stopApplyPoll();
	clearTimeout(boardSearchTimer);
	clearTimeout(decidedSearchTimer);
});
</script>
