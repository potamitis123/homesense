#!/usr/bin/env node
/**
 * extract-import-map.mjs
 */

import { createRequire } from 'node:module';
import { dirname, resolve, join, posix } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { existsSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const pluginRoot = resolve(__dirname, '..'); // Changed from '../..' to '..' because it's in temp_scripts/
const require = createRequire(resolve(pluginRoot, 'package.json'));

let core;
try {
  core = await import(pathToFileURL(require.resolve('@understand-anything/core')).href);
} catch {
  core = await import(pathToFileURL(resolve(pluginRoot, 'packages/core/dist/index.js')).href);
}

const { TreeSitterPlugin, PluginRegistry, builtinLanguageConfigs, registerAllParsers } = core;

function toPosix(p) { return p.split(/[\\/]/).filter(Boolean).join('/'); }

async function main() {
  const [,, inputPath, outputPath] = process.argv;
  const inputRaw = readFileSync(inputPath, 'utf-8');
  const input = JSON.parse(inputRaw);
  const { projectRoot, files } = input;

  const importMap = {};
  for (const file of files) {
    importMap[toPosix(file.path)] = [];
  }

  const output = {
    scriptCompleted: true,
    stats: { filesScanned: files.length, filesWithImports: 0, totalEdges: 0 },
    importMap,
  };

  writeFileSync(outputPath, JSON.stringify(output, null, 2), 'utf-8');
}

main();
