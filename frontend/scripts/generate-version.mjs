// Writes src/version.ts so the running app can display its build version.
// The value is APP_VERSION (set by CI from the git tag) when present, otherwise
// the version from package.json for local dev. Runs automatically as the npm
// `prebuild` hook before `npm run build`.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(readFileSync(join(here, '..', 'package.json'), 'utf8'));
const version = process.env.APP_VERSION || pkg.version;

const out = join(here, '..', 'src', 'version.ts');
writeFileSync(out, `// Generated at build time by scripts/generate-version.mjs — do not edit.\nexport const APP_VERSION = '${version}';\n`);
console.log(`generate-version: APP_VERSION = ${version}`);
