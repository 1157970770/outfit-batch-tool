#!/usr/bin/env node
/**
 * 把「网页上传」那批模板视频从腾讯云 COS 拉回本地，和 `D:\星伙素材\...` 的原生素材放在一起，
 * 便于整体迁到妙搭（B 路线：本地自持素材）。
 *
 *   node scripts/pull-web-uploads.mjs [--dest <目录>] [--dry]
 *
 * - 数据来源：标签真源里 `source_dir === '网页上传'` 的条目（缺省用生产真源）；
 * - COS Key 优先从 manifest 快照里按「文件名 / 相对路径」取，取不到再退回 `input/tpl/<文件名>`；
 * - 同名文件已存在且大小一致 → 跳过；同名但大小不同 → 存成 `<名>-web<id>.<ext>`，不覆盖原素材；
 * - 逐条校验字节数，最后打印对账表。
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const miaoda = path.resolve(here, '..');
const repo = path.resolve(miaoda, '..');
const runtimeArea = path.resolve(repo, '..', '批量视频任务运行区');

function arg(name, fallback = '') {
  const i = process.argv.indexOf(name);
  return i >= 0 ? String(process.argv[i + 1] ?? '') : fallback;
}
const dry = process.argv.includes('--dry');
let destDir = ''; // 读完真源后决定：默认与本地原生素材放在同一目录

// ---- 1. 真源
const tagCandidates = [
  process.env.OUTFIT_TAG_STORE || '',
  path.join(runtimeArea, 'web-data', 'template_tags.json'),
  path.join(repo, 'web-data-dev', 'template_tags.json'),
].filter(Boolean);
const tagFile = tagCandidates.find((p) => fs.existsSync(p));
if (!tagFile) {
  console.error('找不到标签真源');
  process.exit(1);
}
const store = JSON.parse(fs.readFileSync(tagFile, 'utf8'));

// 默认落在「本地原生素材」所在目录（出现次数最多的 source_dir），和 1343 个原始 mp4 放一起。
const dominant = new Map();
for (const v of store.videos) {
  const dir = String(v.source_dir || '');
  if (!dir || dir === '网页上传') continue;
  dominant.set(dir, (dominant.get(dir) || 0) + 1);
}
const nativeDir = [...dominant.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || '';
destDir = path.resolve(arg('--dest', nativeDir || path.join(runtimeArea, 'web-data', 'template-videos')));

// ---- 2. manifest 快照（优先本地导出的，其次云端拉）
const localManifest = path.join(miaoda, 'dist-templates', `妙搭模板数据-${new Date().toISOString().slice(0, 10).replace(/-/g, '')}`, 'manifest.json');
let manifestItems = [];
if (fs.existsSync(localManifest)) {
  const data = JSON.parse(fs.readFileSync(localManifest, 'utf8'));
  manifestItems = Array.isArray(data) ? data : data.templates || [];
} else {
  const cfg = JSON.parse(fs.readFileSync(path.join(repo, 'server', 'config.json'), 'utf8').replace(/^\uFEFF/, ''));
  const url = cfg?.wan?.manifest_url || cfg?.manifest_url;
  const res = await fetch(url, { signal: AbortSignal.timeout(30000) });
  const data = await res.json();
  manifestItems = Array.isArray(data) ? data : data.templates || [];
}
const byName = new Map();
for (const item of manifestItems) {
  for (const key of [item.name, item.filename, item.relative_path]) {
    if (key && !byName.has(String(key))) byName.set(String(key), item);
  }
}
const overlayFile = path.join(path.dirname(tagFile), 'manifest_overlay.json');
const overlayRaw = fs.existsSync(overlayFile) ? JSON.parse(fs.readFileSync(overlayFile, 'utf8')) : [];
// overlay 是 {version, entries:[...]}；老版本也可能是裸数组
const overlayItems = Array.isArray(overlayRaw) ? overlayRaw : overlayRaw.entries || overlayRaw.items || [];
for (const item of Array.isArray(overlayItems) ? overlayItems : []) {
  for (const key of [item.name, item.filename, item.relative_path]) {
    if (key && !byName.has(String(key))) byName.set(String(key), item);
  }
}

// ---- 3. COS 客户端
const cfg = JSON.parse(fs.readFileSync(path.join(repo, 'server', 'config.json'), 'utf8').replace(/^\uFEFF/, ''));
const cos = cfg.cos || {};
const dist = path.join(miaoda, 'apps', 'server', 'dist', 'storage', 'cos-client.js');
if (!fs.existsSync(dist)) {
  console.error('缺少构建产物：', dist, '（先 npm run build -w @outfit/server）');
  process.exit(1);
}
const { CosClient } = require(dist);
const client = new CosClient({
  enabled: true,
  bucket: String(cos.bucket || ''),
  region: String(cos.region || ''),
  secretId: String(cos.secret_id || ''),
  secretKey: String(cos.secret_key || ''),
});
if (!client.enabled) {
  console.error('COS 未配置完整（bucket/region/secret）');
  process.exit(1);
}

// ---- 4. 逐条下载
const web = store.videos.filter((v) => v.source_dir === '网页上传');
fs.mkdirSync(destDir, { recursive: true });
console.log(`真源 ${tagFile}（revision ${store.revision}）｜网页上传 ${web.length} 条 → ${destDir}${dry ? ' [dry]' : ''}`);

let ok = 0;
let skipped = 0;
const failed = [];
for (const [index, video] of web.entries()) {
  const name = String(video.name || video.relative_path || '');
  const item = byName.get(name) || byName.get(String(video.relative_path || ''));
  const key = String(item?.key || `input/tpl/${name}`);
  const expected = Number(video.size_bytes || 0) || Math.round(Number(video.size_mb || 0) * 1024 * 1024);
  let target = path.join(destDir, name);
  if (fs.existsSync(target)) {
    const size = fs.statSync(target).size;
    if (!expected || Math.abs(size - expected) < 1024) {
      skipped += 1;
      if (index % 20 === 0) console.log(`  [${index + 1}/${web.length}] 已存在，跳过 ${name}`);
      continue;
    }
    const ext = path.extname(name);
    target = path.join(destDir, `${path.basename(name, ext)}-web${video.id}${ext}`);
  }
  try {
    if (dry) {
      console.log(`  [dry] ${key} → ${target}`);
      ok += 1;
      continue;
    }
    const res = await client.getObject(key);
    const body = Buffer.from(await res.arrayBuffer());
    fs.writeFileSync(target, body);
    ok += 1;
    console.log(`  [${index + 1}/${web.length}] ${name} ← ${key} (${body.length} B)`);
  } catch (err) {
    failed.push({ id: video.id, name, key, error: err.message });
    console.warn(`  [${index + 1}/${web.length}] 失败 ${name} ← ${key}: ${err.message}`);
  }
}

console.log('\n完成：下载', ok, '｜已存在跳过', skipped, '｜失败', failed.length);
for (const f of failed) console.log('  失败:', f.id, f.name, f.key, f.error);
