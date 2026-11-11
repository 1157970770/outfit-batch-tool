#!/usr/bin/env node
/**
 * 导出「模板数据包」：把妙搭实例真正需要的两份数据打成可上传的目录（可选 zip）。
 *
 *   node scripts/export-templates.mjs [--source production|dev] [--out <dir>] [--zip]
 *
 * 产出：
 *   template_tags.json      标签唯一真源（视频清单 + 标签库 + 弃用/评分/审计）
 *   manifest.json           云端模板清单快照（含每条的 url / key，妙搭离线也能用）
 *   manifest_overlay.json   网页上传视频的地址补丁（存在才拷）
 *   config.template.json    只留 wan/cos/manifest 相关键的配置骨架（不含明文密钥）
 *   README.md               放置位置与自检步骤
 *
 * 注意：视频本体不在这里。默认继续用腾讯云 COS 上的 `input/tpl/<文件名>`；
 * 要在妙搭本地自持素材，加 `--download`（约 18GB，走 COS 客户端逐条下载）。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const miaoda = path.resolve(here, '..');
const repo = path.resolve(miaoda, '..');

function arg(name, fallback = '') {
  const i = process.argv.indexOf(name);
  return i >= 0 ? String(process.argv[i + 1] ?? '') : fallback;
}
const source = arg('--source', 'production');
const wantZip = process.argv.includes('--zip');
const wantDownload = process.argv.includes('--download');

// 真源可能在三个地方：本目录（设计区）、运行区（生产）、或显式环境变量。
const runtimeArea = path.resolve(repo, '..', '批量视频任务运行区');
const candidates = {
  production: [
    process.env.OUTFIT_TAG_STORE || '',
    path.join(runtimeArea, 'web-data', 'template_tags.json'),
    path.join(repo, 'web-data', 'template_tags.json'),
    path.join(repo, 'web-data-dev', 'template_tags.json'),
  ],
  dev: [
    process.env.OUTFIT_TAG_STORE || '',
    path.join(repo, 'web-data-dev', 'template_tags.json'),
    path.join(repo, 'web-data', 'template_tags.json'),
    path.join(runtimeArea, 'web-data', 'template_tags.json'),
  ],
};
const tagFile = (candidates[source] ?? candidates.production).find((p) => p && fs.existsSync(p)) ?? '';
const resolvedTag = tagFile;
if (!fs.existsSync(resolvedTag)) {
  console.error('找不到标签真源：', tagFile);
  process.exit(1);
}

const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '');
const outDir = path.resolve(arg('--out', path.join(miaoda, 'dist-templates', `妙搭模板数据-${stamp}`)));
fs.mkdirSync(outDir, { recursive: true });

// 1) 真源
const store = JSON.parse(fs.readFileSync(resolvedTag, 'utf8'));
fs.copyFileSync(resolvedTag, path.join(outDir, 'template_tags.json'));

// 2) 云端清单快照
const cfgFile = path.join(repo, 'server', 'config.json');
const cfg = fs.existsSync(cfgFile) ? JSON.parse(fs.readFileSync(cfgFile, 'utf8').replace(/^\uFEFF/, '')) : {};
const manifestUrl = cfg?.wan?.manifest_url || cfg?.manifest_url || '';
let manifestCount = 0;
if (manifestUrl) {
  try {
    const res = await fetch(manifestUrl, { signal: AbortSignal.timeout(30000) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const text = await res.text();
    const data = JSON.parse(text);
    const items = Array.isArray(data) ? data : data.templates;
    manifestCount = Array.isArray(items) ? items.length : 0;
    fs.writeFileSync(path.join(outDir, 'manifest.json'), JSON.stringify(data), 'utf8');
    console.log(`manifest.json: ${manifestCount} 条（来源 ${manifestUrl}）`);
  } catch (err) {
    console.warn('拉取 manifest 失败（妙搭启动时会自己再拉）：', err.message);
  }
} else {
  console.warn('config.json 里没有 manifest_url，跳过 manifest.json');
}

// 3) overlay
const overlayFile = path.join(path.dirname(resolvedTag), 'manifest_overlay.json');
if (fs.existsSync(overlayFile)) {
  fs.copyFileSync(overlayFile, path.join(outDir, 'manifest_overlay.json'));
}

// 4) 配置骨架（不带任何密钥明文）
const skeleton = {
  _说明: '妙搭实例用的模板相关配置骨架；密钥请另填，别提交进仓库。',
  manifest_url: manifestUrl ? './manifest.json' : '',
  wan: {
    provider: cfg?.wan?.provider ?? 'dashscope',
    model: cfg?.wan?.model ?? 'wan3.0-video',
    base_url: cfg?.wan?.base_url ?? 'https://dashscope.aliyuncs.com/api/v1',
    region: cfg?.wan?.region ?? 'cn-beijing',
    api_key: '',
    poll_interval: cfg?.wan?.poll_interval ?? 15,
    poll_timeout: cfg?.wan?.poll_timeout ?? 1800,
    max_concurrency: cfg?.wan?.max_concurrency ?? 100,
    query_rpm: cfg?.wan?.query_rpm ?? 250,
    query_rps: cfg?.wan?.query_rps ?? 15,
    max_inflight: cfg?.wan?.max_inflight ?? 100,
  },
  cos: {
    enabled: Boolean(cfg?.cos?.enabled),
    bucket: cfg?.cos?.bucket ?? '',
    region: cfg?.cos?.region ?? '',
    prefix: cfg?.cos?.prefix ?? '',
    domain: cfg?.cos?.domain ?? '',
    secret_id: '',
    secret_key: '',
  },
};
fs.writeFileSync(path.join(outDir, 'config.template.json'), JSON.stringify(skeleton, null, 2), 'utf8');

// 5) README
const stats = (() => {
  const videos = Array.isArray(store.videos) ? store.videos : [];
  const usable = videos.filter((v) => !v.deprecated && !v.missing).length;
  const bytes = videos.reduce((n, v) => n + Number(v.size_bytes || Number(v.size_mb) * 1024 * 1024 || 0), 0);
  return { videos: videos.length, usable, tags: store.library?.tags?.length ?? 0, gb: Math.round((bytes / 1024 ** 3) * 10) / 10 };
})();

const readme = `# 妙搭模板数据包（${stamp}）

真源：\`${resolvedTag}\`
revision：${store.revision} ｜ 视频：${stats.videos}（可用 ${stats.usable}）｜ 标签：${stats.tags} ｜ 视频总量：约 ${stats.gb} GB
${manifestCount ? `manifest 条目：${manifestCount}\n` : ''}
## 里面是什么

| 文件 | 用途 |
| --- | --- |
| \`template_tags.json\` | 标签唯一真源：视频清单、标签库、弃用/评分/审计。**必须**放进妙搭的数据目录 |
| \`manifest.json\` | 云端模板清单快照（每条含 \`url\`/\`key\`）。放进数据目录后把 \`config.json.manifest_url\` 指成 \`./manifest.json\`，妙搭就不必再出网拉清单 |
| \`manifest_overlay.json\` | 网页上传视频的地址补丁（有才拷） |
| \`config.template.json\` | 配置骨架，密钥留空，按需合并进 \`server/config.json\` |

## 视频本体在哪

1. **默认：继续用腾讯云 COS**（推荐）。模板视频已经在 \`input/tpl/<文件名>\`，\`manifest.json\` 里的 \`url\` 是公有读地址；
   妙搭只要能访问外网即可播放/生成，**不需要上传 18GB 视频**。
2. 若妙搭不能出网：把 COS 桶换成妙搭可访问的存储/域名，重新生成 \`manifest.json\` 的 \`url\`；
   或用 \`node scripts/export-templates.mjs --download\` 先把视频拉到本地再上传到妙搭的持久卷。

## 放到妙搭哪里

\`\`\`
<OUTFIT_DATA_ROOT>/
  template_tags.json
  manifest.json
  manifest_overlay.json
\`\`\`

\`OUTFIT_DATA_ROOT\` 就是这个应用的数据目录（默认 \`miaoda/../web-data-dev\`，妙搭上建议挂持久卷并显式配置）。

## 自检

1. \`GET /api/bootstrap\` → \`service.templates_count\` 应等于可用模板数（本包 ${stats.usable} 左右）
2. 管理端 →「模板健康」：清单行数 / 已打标 / 云端就绪 三项不为 0
3. 视频库 → 任选一条 → 右侧预览能播放（说明 manifest 的 url 可访问）
4. 小批量试跑提交 1 条，看成片是否落盘
`;
fs.writeFileSync(path.join(outDir, 'README.md'), readme, 'utf8');

// 6) 可选：把视频本体也拉下来（很慢，默认不做）
if (wantDownload) {
  console.log('--download 尚未实现（需要 COS 客户端逐条下载，约 18GB）。需要时用 apps/server 的 CosClient 写一次性脚本。');
}

console.log('\n输出目录：', outDir);
for (const f of fs.readdirSync(outDir)) {
  console.log('  ', f, fs.statSync(path.join(outDir, f)).size, 'bytes');
}
if (wantZip) {
  const { execFileSync } = await import('node:child_process');
  const zip = outDir + '.zip';
  fs.rmSync(zip, { force: true });
  execFileSync('powershell', ['-NoProfile', '-Command', `Compress-Archive -Path "${outDir}\\*" -DestinationPath "${zip}" -Force`]);
  console.log('zip:', zip, fs.statSync(zip).size, 'bytes');
}
