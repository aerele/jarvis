#!/usr/bin/env node
// Structurally deletes dead .jv-* rules from settings.css using postcss (not
// regex/sed — a prior regex-based attempt corrupted this file when a comment
// swallowed the following selector).
//
// A rule (or one comma-branch of a rule's selector list) is deleted as soon
// as ANY .jv-* class token appearing in that selector is in the dead set
// read from deadClassesFile, not only when every class in it is dead. This
// correctly handles compound/descendant selectors like
// ".jv-dark .jv-settings-navitem.on": jv-dark is a live, widely-used
// dark-mode scope class, but the rule as a whole can never match anything
// because .jv-settings-navitem (the other required part of the compound)
// never appears in the live DOM, so the whole selector is still dead and
// the rule is removed even though .jv-dark itself is very much alive.
//
// After rule removal, any @media block left with no rules and no comments
// is dropped too. @keyframes are never touched by class-based removal since
// their step selectors (from/to/N%) contain no .jv-* tokens — this matters
// because @keyframes jv-popin, though only reached via the (dead) .jv-settings
// rule from *within* this file, is also consumed by SkillDetail.vue's own
// `animation: jv-popin` (confirmed live, see PR description), so it must
// survive even though every settings.css rule using it is being deleted.

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import postcss from "postcss";

const CSS_FILE = process.argv[2] || "src/assets/settings.css";
// No default path ships in the repo on purpose: the dead-class list is a
// derived artifact of scan-jv-classes.mjs's output, not something to keep in
// sync by hand. To reproduce: run
//   node scan-jv-classes.mjs src src/assets/settings.css
// take everything under "=== DEAD CLASSES ===", strip the trailing
// "[only matched inside another file's own <style> block]" notes, and drop
// the four jv-fade-* lines (runtime-derived by <transition name="jv-fade">
// in ChatView.vue, so they're live despite showing up dead here — see the
// PR description). Save the remaining class names one per line, then pass
// that file's path as this script's second argument.
const DEAD_FILE = process.argv[3];
if (!DEAD_FILE || !existsSync(DEAD_FILE)) {
  console.error(
    "Usage: node prune-dead-css.mjs <settings.css path> <dead-classes.txt path>\n" +
      "See the comment at the top of this file for how to (re)generate the dead-classes file."
  );
  process.exit(1);
}

const dead = new Set(
  readFileSync(DEAD_FILE, "utf8").trim().split("\n").filter(Boolean)
);

function splitSelectorList(selector) {
  const parts = [];
  let depth = 0;
  let cur = "";
  for (const ch of selector) {
    if (ch === "(") depth++;
    if (ch === ")") depth--;
    if (ch === "," && depth === 0) {
      parts.push(cur);
      cur = "";
    } else {
      cur += ch;
    }
  }
  if (cur.trim()) parts.push(cur);
  return parts;
}

function classesIn(selector) {
  return (selector.match(/\.jv-[a-zA-Z0-9_-]+/g) || []).map((c) => c.slice(1));
}

const css = readFileSync(CSS_FILE, "utf8");
const root = postcss.parse(css, { from: CSS_FILE });

let removedRuleCount = 0;
let trimmedSelectorCount = 0;
const removedSelectors = [];

root.walkRules((rule) => {
  const branches = splitSelectorList(rule.selector);
  const jvBranches = branches.filter((b) => classesIn(b).length > 0);
  if (jvBranches.length === 0) return; // not a .jv-* rule (e.g. keyframe steps)

  const keptBranches = branches.filter((b) => {
    const classes = classesIn(b);
    if (classes.length === 0) return true; // no jv- class in this branch; leave as-is
    // A compound/descendant selector needs EVERY one of its classes to
    // match some real element simultaneously. If even one class token in
    // this branch has zero real usage anywhere (confirmed dead), the
    // whole branch can never match — regardless of how live any other
    // class in the compound is elsewhere (e.g. ".jv-dark .jv-seg button.on"
    // is unreachable because nothing ever has class="jv-seg", even though
    // .jv-dark itself is applied all over the app). So "any dead" kills
    // the branch, not "all dead".
    return !classes.some((c) => dead.has(c));
  });

  if (keptBranches.length === branches.length) return; // nothing dead here

  if (keptBranches.length === 0) {
    removedSelectors.push(rule.selector.trim());
    rule.remove();
    removedRuleCount++;
  } else {
    trimmedSelectorCount++;
    removedSelectors.push(
      `(partial) ${rule.selector.trim()} -> ${keptBranches.join(", ").trim()}`
    );
    rule.selector = keptBranches.join(",\n");
  }
});

// Drop now-empty @media (or other at-rules) blocks: no child nodes left at all
// (covers both rules and any comments that were inside).
let removedAtRules = 0;
root.walkAtRules((atrule) => {
  if (atrule.nodes && atrule.nodes.length === 0) {
    removedAtRules++;
    atrule.remove();
  }
});

writeFileSync(CSS_FILE, root.toString());

console.log(`Removed ${removedRuleCount} whole rules.`);
console.log(
  `Trimmed ${trimmedSelectorCount} rules (partial comma-branch removal).`
);
console.log(`Removed ${removedAtRules} now-empty at-rule blocks.`);
console.log("");
console.log("=== Removed / trimmed selectors ===");
for (const s of removedSelectors) console.log(s);
