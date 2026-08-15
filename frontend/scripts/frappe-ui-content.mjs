/**
 * frappe-ui Tailwind content-glob generator.
 *
 * Problem: globbing ALL of node_modules/frappe-ui/src (the frappe-ui/CRM
 * default) makes Tailwind scan every component in the library — Calendar,
 * Charts, Kanban, etc. — and emit ~2.6 MB of CSS of which ~99% is unused.
 *
 * Fix: compute the set of frappe-ui source files this app can actually
 * render, and only hand those to Tailwind:
 *
 *   1. Scan ./src for `import { ... } from "frappe-ui"` and collect the
 *      imported export names.
 *   2. Resolve each name to its defining file via frappe-ui/src/index.ts
 *      (following `export * from` re-export chains).
 *   3. Walk each seed file's relative imports transitively (components
 *      import each other and shared utils) to build the full closure.
 *   4. Collapse the closure to directory-level globs (a whole component
 *      dir is included if any file in it is reachable). File/dir-level
 *      granularity is the safe trimming unit: dynamic classes in frappe-ui
 *      are built from static theme/variant maps inside the component
 *      source, so scanning the file always yields those strings.
 *
 * src/utils and src/directives are always included wholesale — they are
 * small, mostly logic, and cheap to scan.
 *
 * Safety valve: if anything about the package layout is unexpected (a
 * name that cannot be resolved, a missing index, ...), we log a warning
 * and fall back to scanning all of frappe-ui/src — i.e. worst case is the
 * old oversized bundle, never missing styles.
 *
 * No maintenance needed for normal work: the closure is recomputed on
 * every Tailwind run, so newly imported frappe-ui components are picked
 * up automatically. To inspect the computed globs:
 *
 *   node -e 'import("./scripts/frappe-ui-content.mjs").then(m => console.log(m.frappeUIContentGlobs().join("\n")))'
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  ".."
);
const APP_SRC = path.join(FRONTEND_ROOT, "src");
const FUI_SRC = path.join(FRONTEND_ROOT, "node_modules", "frappe-ui", "src");

const EXTS = [".vue", ".js", ".ts", ".jsx", ".tsx", ".mjs"];
const GLOB_EXTS = "*.{vue,js,ts,jsx,tsx}";

// Everything-included fallback (the pre-optimization behavior).
const FULL_GLOB = [`./node_modules/frappe-ui/src/**/${GLOB_EXTS}`];

function walkFiles(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) walkFiles(p, out);
    else if (EXTS.includes(path.extname(entry.name))) out.push(p);
  }
  return out;
}

/** Resolve a relative import specifier to an actual file, or null. */
function resolveModule(spec, fromDir) {
  const base = path.resolve(fromDir, spec);
  const candidates = [base];
  for (const ext of EXTS) candidates.push(base + ext);
  for (const ext of EXTS) candidates.push(path.join(base, "index" + ext));
  for (const c of candidates) {
    if (fs.existsSync(c) && fs.statSync(c).isFile()) return c;
  }
  return null;
}

/**
 * All relative import/re-export statements in a file, as
 * `{ spec, names }` — `names` holds the named bindings (empty for
 * default/namespace/side-effect/dynamic imports).
 */
function parseImports(content) {
  const imports = [];
  let m;
  const fromRe =
    /(?:import|export)\s+((?:type\s+)?[^'";]*?)\s*from\s*['"](\.[^'"]+)['"]/g;
  while ((m = fromRe.exec(content))) {
    const names = [];
    const braces = /\{([^}]*)\}/.exec(m[1]);
    if (braces) {
      for (const piece of braces[1].split(",")) {
        const original = piece
          .trim()
          .replace(/^type\s+/, "")
          .split(/\s+as\s+/)[0]
          .trim();
        if (original && original !== "default") names.push(original);
      }
    }
    imports.push({ spec: m[2], names });
  }
  const bareRe = /import\s*['"](\.[^'"]+)['"]/g; // side-effect import './y'
  while ((m = bareRe.exec(content))) imports.push({ spec: m[1], names: [] });
  const dynRe = /import\s*\(\s*['"](\.[^'"]+)['"]\s*\)/g; // dynamic import('./y')
  while ((m = dynRe.exec(content))) imports.push({ spec: m[1], names: [] });
  return imports;
}

/** Parse `export ... from` statements of a module. */
function parseReExports(content) {
  const named = new Map(); // exported name -> module spec
  const wildcards = []; // module specs of `export * from`
  let m;
  const namedRe = /export\s*\{([^}]+)\}\s*from\s*['"]([^'"]+)['"]/g;
  while ((m = namedRe.exec(content))) {
    for (const piece of m[1].split(",")) {
      const parts = piece.trim().split(/\s+as\s+/);
      const exported = (parts[1] || parts[0]).trim();
      if (exported) named.set(exported, m[2]);
    }
  }
  const wildRe = /export\s*\*\s*from\s*['"]([^'"]+)['"]/g;
  while ((m = wildRe.exec(content))) wildcards.push(m[1]);
  return { named, wildcards };
}

/** Names locally declared-and-exported by a module (export const/function/class X, export { X }). */
function parseLocalExports(content) {
  const names = new Set();
  let m;
  const declRe =
    /export\s+(?:async\s+)?(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)/g;
  while ((m = declRe.exec(content))) names.add(m[1]);
  const bareRe = /export\s*\{([^}]+)\}(?!\s*from)/g;
  while ((m = bareRe.exec(content))) {
    for (const piece of m[1].split(",")) {
      const parts = piece.trim().split(/\s+as\s+/);
      const exported = (parts[1] || parts[0]).trim();
      if (exported) names.add(exported);
    }
  }
  return names;
}

/** Does `file` (transitively, via re-exports) export `name`? */
function moduleExports(file, name, seen = new Set()) {
  if (seen.has(file)) return false;
  seen.add(file);
  const content = fs.readFileSync(file, "utf8");
  if (file.endsWith(".vue")) return false; // .vue only has a default export
  if (parseLocalExports(content).has(name)) return true;
  const { named, wildcards } = parseReExports(content);
  if (named.has(name)) return true;
  for (const spec of wildcards) {
    const resolved = resolveModule(spec, path.dirname(file));
    if (resolved && moduleExports(resolved, name, seen)) return true;
  }
  return false;
}

/** Names imported from "frappe-ui" anywhere in the app source. */
function usedFrappeUINames() {
  const names = new Set();
  const re = /import\s*(?:type\s*)?\{([^}]+)\}\s*from\s*['"]frappe-ui['"]/g;
  for (const file of walkFiles(APP_SRC)) {
    const content = fs.readFileSync(file, "utf8");
    let m;
    while ((m = re.exec(content))) {
      for (const piece of m[1].split(",")) {
        const original = piece
          .trim()
          .replace(/^type\s+/, "")
          .split(/\s+as\s+/)[0]
          .trim();
        if (original) names.add(original);
      }
    }
  }
  return names;
}

export function frappeUIContentGlobs() {
  try {
    const indexFile = resolveModule("./index", FUI_SRC);
    if (!indexFile) throw new Error("frappe-ui/src/index.ts not found");
    const indexContent = fs.readFileSync(indexFile, "utf8");
    const { named, wildcards } = parseReExports(indexContent);

    // Pre-resolve wildcard re-export modules of the package entry.
    const wildcardFiles = wildcards
      .map((spec) => resolveModule(spec, FUI_SRC))
      .filter(Boolean);

    /** Resolve an export name of the "frappe-ui" barrel to its defining file. */
    // const, not a declaration: a function declaration inside a block is
    // no-inner-declarations. Both call sites are below this line, so nothing
    // relied on hoisting.
    const resolveName = (name) => {
      const file = named.has(name)
        ? resolveModule(named.get(name), FUI_SRC)
        : wildcardFiles.find((f) => moduleExports(f, name)) || null;
      if (!file) throw new Error(`cannot resolve frappe-ui export "${name}"`);
      return file;
    };

    // 1. Seed files: the defining module of every used export. (index.ts
    //    itself is only the name-resolution table — seeding it would pull
    //    every component in the library back into the closure.)
    const seeds = new Set();
    for (const name of usedFrappeUINames()) seeds.add(resolveName(name));

    // utils + directives are always scanned; their imports join the closure.
    for (const dir of ["utils", "directives"]) {
      const p = path.join(FUI_SRC, dir);
      if (fs.existsSync(p)) walkFiles(p).forEach((f) => seeds.add(f));
    }

    // 2. Transitive closure over relative imports within frappe-ui/src.
    //    Some frappe-ui files import siblings through the package barrel
    //    ('../../index'); follow only the names they import, not the
    //    whole barrel.
    const reachable = new Set();
    const queue = [...seeds];
    while (queue.length) {
      const file = queue.pop();
      if (reachable.has(file)) continue;
      reachable.add(file);
      const content = fs.readFileSync(file, "utf8");
      for (const { spec, names } of parseImports(content)) {
        const resolved = resolveModule(spec, path.dirname(file));
        if (!resolved || !resolved.startsWith(FUI_SRC)) continue;
        if (resolved === indexFile) {
          if (!names.length)
            throw new Error(`unresolvable barrel import in ${file}`);
          for (const name of names) queue.push(resolveName(name));
        } else if (!reachable.has(resolved)) {
          queue.push(resolved);
        }
      }
    }

    // 3. Collapse to dir-level globs under src/<area>/<name>/, keep
    //    top-level files as-is.
    const globs = new Set();
    for (const file of reachable) {
      const rel = path.relative(FUI_SRC, file).split(path.sep);
      const glob =
        rel.length > 2
          ? [
              ".",
              "node_modules",
              "frappe-ui",
              "src",
              rel[0],
              rel[1],
              "**",
              GLOB_EXTS,
            ].join("/")
          : [".", "node_modules", "frappe-ui", "src", ...rel].join("/");
      globs.add(glob);
    }
    return [...globs].sort();
  } catch (err) {
    console.warn(
      `[frappe-ui-content] ${err.message} — falling back to scanning all of frappe-ui/src (bigger CSS, but nothing breaks)`
    );
    return FULL_GLOB;
  }
}
