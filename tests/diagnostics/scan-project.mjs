#!/usr/bin/env node
/**
 * scan-project.mjs
 */

import { createRequire } from 'node:module';
import { dirname, resolve, join, basename, extname, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import {
  existsSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { spawnSync } from 'node:child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));
const pluginRoot = resolve(__dirname, '..'); // Changed from '../..' to '..' because it's in temp_scripts/
const require = createRequire(resolve(pluginRoot, 'package.json'));

let core;
try {
  core = await import(pathToFileURL(require.resolve('@understand-anything/core')).href);
} catch {
  core = await import(pathToFileURL(resolve(pluginRoot, 'packages/core/dist/index.js')).href);
}

const { createIgnoreFilter } = core;

const LANGUAGE_BY_EXT = Object.freeze({
  '.ts': 'typescript',
  '.tsx': 'typescript',
  '.js': 'javascript',
  '.jsx': 'javascript',
  '.mjs': 'javascript',
  '.cjs': 'javascript',
  '.py': 'python',
  '.pyi': 'python',
  '.go': 'go',
  '.rs': 'rust',
  '.java': 'java',
  '.kt': 'kotlin',
  '.kts': 'kotlin',
  '.cs': 'csharp',
  '.swift': 'swift',
  '.lua': 'lua',
  '.rb': 'ruby',
  '.rake': 'ruby',
  '.php': 'php',
  '.c': 'c',
  '.h': 'c',
  '.cpp': 'cpp',
  '.cc': 'cpp',
  '.cxx': 'cpp',
  '.hpp': 'cpp',
  '.hxx': 'cpp',
  '.vue': 'vue',
  '.svelte': 'svelte',
  '.sh': 'shell',
  '.bash': 'shell',
  '.zsh': 'shell',
  '.ps1': 'powershell',
  '.psm1': 'powershell',
  '.psd1': 'powershell',
  '.bat': 'batch',
  '.cmd': 'batch',
  '.html': 'html',
  '.htm': 'html',
  '.css': 'css',
  '.scss': 'css',
  '.sass': 'css',
  '.less': 'css',
  '.md': 'markdown',
  '.mdx': 'markdown',
  '.rst': 'markdown',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.json': 'json',
  '.jsonc': 'jsonc',
  '.toml': 'toml',
  '.xml': 'xml',
  '.xsl': 'xml',
  '.xsd': 'xml',
  '.plist': 'xml',
  '.cfg': 'config',
  '.ini': 'config',
  '.env': 'config',
  '.sql': 'sql',
  '.graphql': 'graphql',
  '.gql': 'graphql',
  '.proto': 'protobuf',
  '.prisma': 'prisma',
  '.csv': 'csv',
  '.tsv': 'csv',
  '.tf': 'terraform',
  '.tfvars': 'terraform',
  '.gradle': 'gradle',
  '.csproj': 'csproj',
  '.sln': 'sln',
  '.properties': 'properties',
  '.mod': 'mod',
  '.sum': 'sum',
});

const LANGUAGE_BY_FILENAME = Object.freeze({
  Dockerfile: 'dockerfile',
  Makefile: 'makefile',
  GNUmakefile: 'makefile',
  makefile: 'makefile',
  Jenkinsfile: 'jenkinsfile',
  Procfile: 'procfile',
  Vagrantfile: 'vagrantfile',
});

function dotfileKey(base) {
  if (!base.startsWith('.')) return null;
  const m = base.match(/^(\.[a-z0-9]+)/i);
  return m ? m[1].toLowerCase() : null;
}

export function detectLanguage(filePath) {
  const base = basename(filePath);
  const ext = extname(filePath).toLowerCase();
  if (base === 'Dockerfile' || base.startsWith('Dockerfile.')) return 'dockerfile';
  const dotKey = dotfileKey(base);
  if (dotKey && LANGUAGE_BY_EXT[dotKey]) return LANGUAGE_BY_EXT[dotKey];
  if (ext) {
    const byExt = LANGUAGE_BY_EXT[ext];
    if (byExt) return byExt;
    return ext.slice(1);
  }
  const byFilename = LANGUAGE_BY_FILENAME[base];
  if (byFilename) return byFilename;
  return 'unknown';
}

const CATEGORY_BY_EXT = Object.freeze({
  '.md': 'docs', '.mdx': 'docs', '.rst': 'docs', '.txt': 'docs', '.text': 'docs',
  '.yaml': 'config', '.yml': 'config', '.json': 'config', '.jsonc': 'config', '.toml': 'config',
  '.xml': 'config', '.xsl': 'config', '.xsd': 'config', '.plist': 'config', '.cfg': 'config',
  '.ini': 'config', '.env': 'config', '.properties': 'config', '.csproj': 'config', '.sln': 'config',
  '.mod': 'config', '.sum': 'config', '.gradle': 'config',
  '.tf': 'infra', '.tfvars': 'infra',
  '.sql': 'data', '.graphql': 'data', '.gql': 'data', '.proto': 'data', '.prisma': 'data', '.csv': 'data', '.tsv': 'data',
  '.sh': 'script', '.bash': 'script', '.zsh': 'script', '.ps1': 'script', '.psm1': 'script', '.psd1': 'script', '.bat': 'script', '.cmd': 'script',
  '.html': 'markup', '.htm': 'markup', '.css': 'markup', '.scss': 'markup', '.sass': 'markup', '.less': 'markup',
});

const INFRA_FILENAMES = new Set([
  'Dockerfile', '.dockerignore', 'Makefile', 'GNUmakefile', 'makefile', 'Jenkinsfile', 'Procfile', 'Vagrantfile', '.gitlab-ci.yml',
]);

export function detectCategory(filePath) {
  const base = basename(filePath);
  const ext = extname(filePath).toLowerCase();
  const posix = filePath.split(sep).join('/');
  if (base === 'LICENSE') return 'code';
  if (INFRA_FILENAMES.has(base)) return 'infra';
  if (base === 'Dockerfile' || base.startsWith('Dockerfile.')) return 'infra';
  if (base.startsWith('docker-compose.')) return 'infra';
  if (base === 'compose.yml' || base === 'compose.yaml') return 'infra';
  if (posix.startsWith('.github/workflows/')) return 'infra';
  if (posix.startsWith('.circleci/')) return 'infra';
  if (/(^|\/)(k8s|kubernetes)\//.test(posix)) return 'infra';
  if (/\.k8s\.(ya?ml)$/i.test(base)) return 'infra';
  if (ext) {
    const byExt = CATEGORY_BY_EXT[ext];
    if (byExt) return byExt;
  }
  const dotKey = dotfileKey(base);
  if (dotKey) {
    const byDot = CATEGORY_BY_EXT[dotKey];
    if (byDot) return byDot;
  }
  return 'code';
}

export function estimateComplexity(totalFiles) {
  if (totalFiles <= 30) return 'small';
  if (totalFiles <= 150) return 'moderate';
  if (totalFiles <= 500) return 'large';
  return 'very-large';
}

function toPosix(p) { return p.split(sep).join('/'); }

function enumerateViaGit(projectRoot) {
  const result = spawnSync('git', ['ls-files', '-z', '-co', '--exclude-standard'], {
    cwd: projectRoot,
    encoding: 'utf-8',
    maxBuffer: 256 * 1024 * 1024,
  });
  if (result.status !== 0 || !result.stdout) return null;
  return result.stdout.split('\0').filter(Boolean).map(toPosix);
}

function enumerateViaWalk(projectRoot) {
  const HARD_SKIP_DIRS = new Set(['node_modules', '.git', '.svn', '.hg', '__pycache__', '.pytest_cache']);
  const out = [];
  function walk(absDir) {
    let entries;
    try { entries = readdirSync(absDir, { withFileTypes: true }); } catch (err) { return; }
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const ent of entries) {
      if (ent.isDirectory()) {
        if (HARD_SKIP_DIRS.has(ent.name)) continue;
        walk(join(absDir, ent.name));
      } else if (ent.isFile()) {
        const rel = toPosix(relative(projectRoot, join(absDir, ent.name)));
        if (rel) out.push(rel);
      }
    }
  }
  walk(projectRoot);
  return out;
}

function enumerateFiles(projectRoot) {
  const fromGit = enumerateViaGit(projectRoot);
  if (fromGit !== null) return fromGit;
  return enumerateViaWalk(projectRoot);
}

function countLines(absPath, posixPath) {
  try {
    const buf = readFileSync(absPath);
    let count = 0;
    for (let i = 0; i < buf.length; i++) { if (buf[i] === 0x0a) count++; }
    return count;
  } catch (err) { return null; }
}

async function main() {
  const [, , projectRoot, outputPath] = process.argv;
  const candidates = enumerateFiles(projectRoot);
  const combined = createIgnoreFilter(projectRoot);
  const fileEntries = [];
  for (const rel of candidates) {
    if (combined.isIgnored(rel)) continue;
    const absPath = join(projectRoot, rel);
    const sizeLines = countLines(absPath, rel);
    if (sizeLines === null) continue;
    fileEntries.push({
      path: rel,
      language: detectLanguage(rel),
      sizeLines,
      fileCategory: detectCategory(rel),
    });
  }
  fileEntries.sort((a, b) => a.path.localeCompare(b.path));
  const byCategory = {};
  const byLanguage = {};
  for (const f of fileEntries) {
    byCategory[f.fileCategory] = (byCategory[f.fileCategory] || 0) + 1;
    byLanguage[f.language] = (byLanguage[f.language] || 0) + 1;
  }
  const estimatedComplexity = estimateComplexity(fileEntries.length);
  const output = {
    scriptCompleted: true,
    files: fileEntries,
    totalFiles: fileEntries.length,
    filteredByIgnore: 0,
    estimatedComplexity,
    stats: { filesScanned: fileEntries.length, byCategory, byLanguage },
  };
  writeFileSync(outputPath, JSON.stringify(output, null, 2), 'utf-8');
}

main();
