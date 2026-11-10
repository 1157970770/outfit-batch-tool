import { useCallback, useEffect, useState, type ReactNode } from 'react';

import { Placeholder } from '@/ui/Placeholder';

const VIEW_STORAGE_KEY = 'outfit.view';

/** 导航顺序与原版 index.html 一致；canvas 是最后一页，material 用 iframe。 */
export const VIEWS = [
  { key: 'create', label: '小批量试跑', admin: false },
  { key: 'batch', label: '大批量生产', admin: false },
  { key: 'history', label: '历史任务', admin: false },
  { key: 'admin', label: '管理', admin: true },
  { key: 'tag', label: '视频库', admin: false },
  { key: 'material', label: '素材检索', admin: false },
  { key: 'ps', label: 'PS 助手', admin: false },
  { key: 'imagegen', label: '生图', admin: false },
  { key: 'canvas', label: '画布', admin: false },
] as const;

export type ViewKey = (typeof VIEWS)[number]['key'];

const RESTORE_VIEWS: ViewKey[] = ['create', 'history', 'admin', 'tag', 'material', 'ps', 'imagegen', 'canvas'];

export function useView(defaultKey: ViewKey = 'create'): [ViewKey, (key: ViewKey) => void] {
  const [view, setView] = useState<ViewKey>(() => {
    const saved = window.localStorage.getItem(VIEW_STORAGE_KEY) as ViewKey | null;
    return saved && RESTORE_VIEWS.includes(saved) ? saved : defaultKey;
  });

  const change = useCallback((key: ViewKey) => {
    setView(key);
    try {
      window.localStorage.setItem(VIEW_STORAGE_KEY, key);
    } catch {
      /* 隐私模式下忽略 */
    }
  }, []);

  useEffect(() => {
    const onHash = () => {
      const key = window.location.hash.replace('#', '') as ViewKey;
      if (RESTORE_VIEWS.includes(key)) setView(key);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  return [view, change];
}

/** 每一页的外壳：保留 `view` + `active-view` 两个类名，CSS 与旧版选择器保持兼容。 */
export function ViewSection({
  id,
  active,
  children,
}: {
  id: string;
  active: boolean;
  children: ReactNode;
}) {
  return (
    <section id={`${id}-view`} className={`view${active ? ' active-view' : ''}`} hidden={!active}>
      {children}
    </section>
  );
}

export function CreatePlaceholder() {
  return (
    <Placeholder
      title="小批量试跑"
      phase="P1"
      summary="1 张定妆图 + 最多 9 张细节图 + N 个模板 → N 条 7 秒视频。P1 会把图片槽位、场景参数、提示词与提交链路整体搬过来。"
      bullets={[
        '图片槽位：槽位 1 定妆图必填，2–10 细节图可选，删除后序号左移，图N = upload_ids[N-1]',
        '上传：3 并发、250ms 进度刷新，单槽失败不阻塞其他槽位',
        '提交后 3500ms 轮询任务详情，终态包含 succeeded / partial / failed / canceled / interrupted',
      ]}
    />
  );
}

export function BatchPlaceholder() {
  return (
    <Placeholder
      title="大批量生产"
      phase="P2"
      summary="批次四步向导：新建 → 选模板并锁定 → 确认与槽位管理 → 多轮生产。状态机 draft / locked / closed，条目 pending / producing / done / failed。"
      bullets={[
        '候选模板带标签三态筛选、评分下限、已用过滤与客户端分页',
        '锁定后只能增删细节图，主图不可换',
        '生产轮次 5/10/20/50/100，单批上限 500，单轮上限 100',
      ]}
    />
  );
}

export function HistoryPlaceholder() {
  return (
    <Placeholder
      title="历史任务"
      phase="P2"
      summary="按「范围 / 账户 / 分组」浏览历史任务，分组支持 owner / day / 未分组三种键；记录行可打开三栏对比（定妆图 + 生成视频 + 模板视频）。"
      bullets={['管理员可切 scope=all 或按 owner 过滤', '整批 ZIP 下载：多轮收集 + 试跑资格判定', '对比层用 portal 挂 body，Esc 与遮罩关闭']}
    />
  );
}

export function TagPlaceholder() {
  return (
    <Placeholder
      title="视频库（打标）"
      phase="P3"
      summary="标签唯一真源 web-data*/template_tags.json：打标、评分、弃用（软删除）、标签库维护、版本快照与回滚。"
      bullets={[
        '写入走 revision 乐观锁，冲突返回 409 并自动重拉',
        '全客户端分页（50/100/200），搜索 300ms 防抖',
        '批量操作与批量下载，弃用不删视频、不删 COS 对象',
      ]}
    />
  );
}

export function AdminPlaceholder() {
  return (
    <Placeholder
      title="管理"
      phase="P3"
      summary="服务状态、归档检索、账户用量（金额只在这里出现）、用户增删与配额。"
      bullets={['/api/admin/status 与 template-health', '账户用量 CSV 导出', '归档检索签名链接', '重建模板索引（reload-templates）']}
    />
  );
}

export function PsPlaceholder() {
  return (
    <Placeholder
      title="PS 助手（去紫）"
      phase="P4"
      summary="对定妆图做去紫处理：LAB 中性色带内降低饱和度上限，强度 1–100，结果按 upload_id + 强度缓存到 web-data*/ps/。"
      bullets={['numpy 版算法 1:1 复刻（缺依赖返回 503）', '双栏对比 + 残差统计', '结果 PNG 与 .json 侧车落盘，可整目录删']}
    />
  );
}

export function ImageGenPlaceholder() {
  return (
    <Placeholder
      title="生图"
      phase="P4"
      summary="独立队列（不占视频在飞名额）：提示词 + 参考图 → GPT Image VIP / Nano Banana 2.1；两套尺寸口径互不通用。"
      bullets={['images 协议按像素，gemini 按「比例@档位」，200 没图也计费不自动重试', '参考图建任务时拷贝到 files/<id>/refN', '2500ms 轮询，仅在有进行中任务时跑']}
    />
  );
}

export function MaterialPlaceholder() {
  return (
    <Placeholder
      title="素材检索"
      phase="P4"
      summary="iframe 内嵌素材检索服务（地址来自 web.material.url，默认 http://192.168.10.179:8700），首次进入才挂载。"
      bullets={['懒挂载，切走不卸载', 'CSP frame-src 必须放行该地址']}
    />
  );
}

export function CanvasPlaceholder() {
  return (
    <Placeholder
      title="画布"
      phase="P5"
      summary="复用现有 canvas/ 工程（React 19 + @xyflow/react + zustand）：节点式编排素材、提示词与生成器。"
      bullets={['保持单 root：包装组件 mount 调 CanvasUI.enter()，unmount 调 leave()', '2500ms 轮询依赖 #canvas-view.active-view 类名', 'canvas.css 含第三方全局类，需要作用域隔离']}
    />
  );
}
