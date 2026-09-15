import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import tsParser from "@typescript-eslint/parser";
import { importX } from "eslint-plugin-import-x";
import { createTypeScriptImportResolver } from "eslint-import-resolver-typescript";

const root = path.dirname(fileURLToPath(import.meta.url));
const mainLayers = ["app", "widgets", "features", "entities", "shared"];
const zones = mainLayers.slice(1).map((layer, index) => ({
  target: path.join(root, "apps/main/src", layer),
  from: mainLayers.slice(0, index + 1).map((higher) => path.join(root, "apps/main/src", higher)),
  message: "Main layers may only depend downwards.",
}));

const mainRoot = path.join(root, "apps/main/src");
const mainFiles = sourceFiles(mainRoot);
const sliceSegments = new Set(["ui", "api", "model", "lib", "config"]);

function sourceFiles(directory) {
  const files = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...sourceFiles(filename));
    else if (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx")) files.push(filename);
  }
  return files;
}

function isWithin(filename, directory) {
  const relative = path.relative(directory, filename);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function sliceRoots(directory) {
  const slices = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const candidate = path.join(directory, entry.name);
    const contents = fs.readdirSync(candidate, { withFileTypes: true });
    if (contents.some((child) => child.name === "index.ts" || (child.isDirectory() && sliceSegments.has(child.name)))) {
      slices.push(candidate);
    } else {
      slices.push(...sliceRoots(candidate));
    }
  }
  return slices;
}

for (const layer of ["widgets", "features", "entities"]) {
  const slices = sliceRoots(path.join(mainRoot, layer));
  for (const slice of slices) {
    const siblings = slices.filter((other) => other !== slice);
    if (siblings.length) {
      zones.push({ target: slice, from: siblings, message: "Main sibling slices are isolated." });
    }
    zones.push({
      target: mainFiles.filter((filename) => !isWithin(filename, slice)),
      from: slice,
      except: ["./index.ts", "./index.server.ts"],
      message: "Use the slice's public entry.",
    });
  }
}

const serverSurface = path.join(mainRoot, "**/*.server.{ts,tsx}");
zones.push({
  target: path.join(mainRoot, "**/index.ts"),
  from: serverSurface,
  message: "Universal entries must not expose server-only code.",
});

const boRoot = path.join(root, "apps/bo/src");
const boApp = path.join(boRoot, "app");
const boFiles = sourceFiles(boRoot);
const routeOwners = new Map();
const privateOwners = new Map();
for (const filename of boFiles) {
  if (!isWithin(filename, boApp)) continue;
  const segments = path.relative(boApp, filename).split(path.sep);
  const privateIndex = segments.findIndex((segment) => segment.startsWith("_"));
  const owner = privateIndex < 0 ? path.dirname(filename) : path.join(boApp, ...segments.slice(0, privateIndex));
  routeOwners.set(filename, owner);
  if (privateIndex >= 0) {
    privateOwners.set(path.join(boApp, ...segments.slice(0, privateIndex + 1)), owner);
  }
}
for (const [directory, owner] of privateOwners) {
  const parentComponents = path.basename(directory) === "_components";
  zones.push({
    target: boFiles.filter((filename) => {
      const importerOwner = routeOwners.get(filename);
      return importerOwner !== owner && !(parentComponents && importerOwner && isWithin(importerOwner, owner));
    }),
    from: directory,
    message: "Route-private code belongs to its route; only parent _components may be consumed by child routes.",
  });
}

const restrictedPaths = importX.rules["no-restricted-paths"];
const clientServerRule = {
  meta: restrictedPaths.meta,
  create(context) {
    const client = context.sourceCode.ast.body.some((statement) => statement.directive === "use client");
    return client ? restrictedPaths.create(context) : {};
  },
};

const frameworkPackages = new Set(["react", "react-dom", "@types/react", "@types/react-dom", "next", "react-hook-form"]);
const packageOwners = new Map();
const rhfTypeHelper = path.join(boApp, "demo/orders/new/_lib/rhf-path.ts");

function packageName(filename) {
  const cached = packageOwners.get(filename);
  if (cached !== undefined) return cached;
  let directory = path.dirname(filename);
  while (true) {
    const manifest = path.join(directory, "package.json");
    if (fs.existsSync(manifest)) {
      const name = JSON.parse(fs.readFileSync(manifest, "utf8")).name;
      if (name) {
        packageOwners.set(filename, name);
        return name;
      }
    }
    const parent = path.dirname(directory);
    if (parent === directory) {
      packageOwners.set(filename, null);
      return null;
    }
    directory = parent;
  }
}

function entirelyTypeOnly(node) {
  return node.importKind === "type" || node.exportKind === "type"
    || (node.specifiers?.length > 0 && node.specifiers.every((specifier) => (specifier.importKind ?? specifier.exportKind) === "type"));
}

const purityRule = {
  meta: {
    type: "problem",
    schema: [],
    messages: {
      forbidden: "{{package}} is not allowed here; the exact RHF helper permits only RHF types.",
      localUi: "Pure BO _lib cannot depend directly on local UI modules, including types.",
      unresolved: "Cannot resolve {{specifier}} to check the BO dependency boundary.",
    },
  },
  create(context) {
    const filename = context.filename;
    const pureLib = isWithin(filename, boRoot) && path.relative(boRoot, filename).split(path.sep).includes("_lib");
    const primitive = isWithin(filename, path.join(boRoot, "ui"));
    if (!pureLib && !primitive) return {};
    const resolver = context.settings["import-x/resolver-next"][0];
    function check(source, typeOnly) {
      const specifier = source?.type === "Literal" ? source.value
        : source?.type === "TemplateLiteral" && source.expressions.length === 0 ? source.quasis[0].value.cooked : null;
      if (typeof specifier !== "string") return;
      const resolved = resolver.resolve(specifier, filename);
      if (!resolved.found) {
        context.report({ node: source, messageId: "unresolved", data: { specifier } });
        return;
      }
      if (!resolved.path) return;
      if (pureLib && isWithin(resolved.path, boRoot)) {
        const segments = path.relative(boRoot, resolved.path).split(path.sep);
        if (segments[0] === "ui" || segments.includes("_components") || segments.includes("_ui")) {
          context.report({ node: source, messageId: "localUi" });
          return;
        }
      }
      const dependency = packageName(resolved.path);
      const forbidden = pureLib ? frameworkPackages.has(dependency) : dependency === "react-hook-form";
      if (!forbidden || (filename === rhfTypeHelper && dependency === "react-hook-form" && typeOnly)) return;
      context.report({ node: source, messageId: "forbidden", data: { package: dependency } });
    }
    return {
      ImportDeclaration(node) { check(node.source, entirelyTypeOnly(node)); },
      ExportNamedDeclaration(node) { check(node.source, entirelyTypeOnly(node)); },
      ExportAllDeclaration(node) { check(node.source, entirelyTypeOnly(node)); },
      ImportExpression(node) { check(node.source, false); },
      TSImportType(node) { check(node.source, true); },
      TSImportEqualsDeclaration(node) {
        if (node.moduleReference.type === "TSExternalModuleReference") {
          check(node.moduleReference.expression, node.importKind === "type");
        }
      },
      CallExpression(node) {
        if (node.callee.type === "Identifier" && node.callee.name === "require" && node.arguments.length === 1) {
          check(node.arguments[0], false);
        }
      },
    };
  },
};

export default [
  {
    basePath: root,
    files: ["**/*.ts", "**/*.tsx"],
    languageOptions: {
      parser: tsParser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    linterOptions: { noInlineConfig: true },
    plugins: { "import-x": importX, mixed: { rules: { "client-server": clientServerRule, purity: purityRule } } },
    settings: {
      "import-x/parsers": { "@typescript-eslint/parser": [".ts", ".tsx"] },
      "import-x/resolver-next": [
        createTypeScriptImportResolver({ project: path.join(root, "tsconfig.json") }),
      ],
    },
    rules: {
      "import-x/no-unresolved": "error",
      "import-x/no-restricted-paths": ["error", { basePath: root, zones }],
      "mixed/client-server": ["error", {
        basePath: root,
        zones: [{ target: mainRoot, from: serverSurface, message: "Client code cannot import a server surface." }],
      }],
      "mixed/purity": "error",
    },
  },
  ...["main", "bo"].map((app) => ({
    basePath: root,
    files: [`apps/${app}/src/**/*.ts`, `apps/${app}/src/**/*.tsx`],
    settings: {
      "import-x/resolver-next": [
        createTypeScriptImportResolver({ project: path.join(root, "apps", app, "tsconfig.json") }),
      ],
    },
  })),
];
