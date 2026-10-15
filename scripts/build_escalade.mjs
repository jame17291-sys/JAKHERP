#!/usr/bin/env node
// Usage: node build_escalade.mjs <escalade-source-root> [receipt-path]
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const common = [["import { dirname, resolve } from 'path';", "const { dirname, resolve } = require('path');"]];
export const modes = [
  {source: 'src/async.js', cjs: 'dist/index.js', esm: 'dist/index.mjs', transforms: [...common,
    ["import { readdir, stat } from 'fs';", "const { readdir, stat } = require('fs');"],
    ["import { promisify } from 'util';", "const { promisify } = require('util');"],
    ['export default async function (start, callback) {', 'module.exports = async function (start, callback) {']]},
  {source: 'src/sync.js', cjs: 'sync/index.js', esm: 'sync/index.mjs', transforms: [...common,
    ["import { readdirSync, statSync } from 'fs';", "const { readdirSync, statSync } = require('fs');"],
    ['export default function (start, callback) {', 'module.exports = function (start, callback) {']]}
];

export function transformSource(original, mode) {
  let output = original;
  for (const [from, to] of mode.transforms) {
    const lines = output.split('\n');
    if (lines.filter(line => line === from).length !== 1) {
      throw new Error(`Expected exactly one anchored source line: ${from}`);
    }
    output = lines.map(line => line === from ? to : line).join('\n');
  }
  if (/^\s*(?:import|export)\b/m.test(output)) throw new Error(`Unconverted module syntax in ${mode.source}`);
  return output;
}

export async function build(packageRoot, receiptPath = path.join(packageRoot, 'source-build-receipt.json')) {
  const pkg = JSON.parse(await readFile(path.join(packageRoot, 'package.json'), 'utf8'));
  if (pkg.name !== 'escalade' || pkg.version !== '3.1.1') throw new Error('Only official escalade 3.1.1 source is supported.');
  const hash = bytes => createHash('sha256').update(bytes).digest('hex');
  const manifest = {
    package: 'escalade', version: '3.1.1', repository: 'https://github.com/lukeed/escalade', tag: 'v3.1.1',
    build: 'Deterministic import/export-only ESM to CommonJS conversion; not the upstream bundt minification build. Original ESM also copied without modification.',
    node: process.version, files: []
  };
  // Validate both modules completely before writing generated entry points.
  const prepared = await Promise.all(modes.map(async mode => {
    const original = await readFile(path.join(packageRoot, mode.source), 'utf8');
    return {mode, original, output: transformSource(original, mode)};
  }));
  for (const {mode, original, output} of prepared) {
    await mkdir(path.dirname(path.join(packageRoot, mode.cjs)), {recursive: true});
    await writeFile(path.join(packageRoot, mode.cjs), output);
    await writeFile(path.join(packageRoot, mode.esm), original);
    manifest.files.push({
      source: mode.source, sourceSha256: hash(original),
      transforms: mode.transforms.map(([from, to]) => ({from, to, scope: 'one complete line only'})),
      cjs: mode.cjs, cjsSha256: hash(output), esm: mode.esm, esmSha256: hash(original)
    });
  }
  await mkdir(path.dirname(receiptPath), {recursive: true});
  await writeFile(receiptPath, JSON.stringify(manifest, null, 2) + '\n');
  return manifest;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (!process.argv[2]) {
    console.error('Usage: node build_escalade.mjs <escalade-source-root> [receipt-path]');
    process.exitCode = 2;
  } else {
    await build(path.resolve(process.argv[2]), process.argv[3] ? path.resolve(process.argv[3]) : undefined);
    console.log('Built escalade 3.1.1; original sources preserved and build receipt written.');
  }
}
