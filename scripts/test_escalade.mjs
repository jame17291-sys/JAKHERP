#!/usr/bin/env node
// Usage: node test_escalade.mjs [source-built-escalade-package-root]
import assert from 'node:assert/strict';
import {access, mkdtemp, mkdir, writeFile, rm} from 'node:fs/promises';
import {createRequire} from 'node:module';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {modes, transformSource} from './build_escalade.mjs';

const require = createRequire(import.meta.url);
const scriptRoot = path.dirname(fileURLToPath(import.meta.url));
const packageRoot = path.resolve(process.argv[2] || path.join(scriptRoot, '../deploy/rtlcss/node_modules/escalade'));
for (const mode of modes) {
  const valid = mode.transforms.map(([from]) => from).join('\n') + '\n}\n';
  assert.throws(() => transformSource('', mode), /Expected exactly one anchored source line/);
  assert.throws(() => transformSource(valid + mode.transforms[0][0] + '\n', mode), /Expected exactly one anchored source line/);
  assert.throws(() => transformSource(valid.replace(mode.transforms[0][0], ' ' + mode.transforms[0][0]), mode), /Expected exactly one anchored source line/);
  assert.throws(() => transformSource(valid + 'export const unexpected = 1;\n', mode), /Unconverted module syntax/);
}
console.log('Exact-transform rejection checks passed.');
try {
  await access(path.join(packageRoot, 'package.json'));
} catch (error) {
  if (error.code !== 'ENOENT' || process.argv[2]) throw error;
  console.log('Runtime checks skipped: source-built deployment escalade package is not present.');
  process.exit(0);
}
assert.equal(require(path.join(packageRoot, 'package.json')).version, '3.1.1');
const asynchronous = require(packageRoot);
const synchronous = require(path.join(packageRoot, 'sync'));
const fixture = await mkdtemp(path.join(os.tmpdir(), 'jakh-escalade-'));
try {
  const nested = path.join(fixture, 'one', 'two');
  const input = path.join(nested, 'input.css');
  const marker = path.join(fixture, 'config.json');
  await mkdir(nested, {recursive: true});
  await writeFile(input, 'body {}');
  await writeFile(marker, '{}');
  const visited = [];
  const find = (dir, files) => { visited.push(dir); return files.includes('config.json') ? 'config.json' : undefined; };
  assert.equal(synchronous(nested, find), marker);
  assert.deepEqual(visited, [nested, path.dirname(nested), fixture]);
  visited.length = 0;
  assert.equal(await asynchronous(input, async (dir, files) => find(dir, files)), marker);
  assert.deepEqual(visited, [nested, path.dirname(nested), fixture]);
  assert.equal(synchronous(input, () => undefined), undefined);
  assert.equal(await asynchronous(nested, async () => undefined), undefined);
  assert.throws(() => synchronous(path.join(fixture, 'missing'), () => undefined), {code: 'ENOENT'});
  await assert.rejects(asynchronous(path.join(fixture, 'missing'), () => undefined), {code: 'ENOENT'});
  console.log('Escalade package runtime checks passed for sync/async traversal, input paths, absent results, and errors.');
} finally {
  await rm(fixture, {recursive: true, force: true});
}
