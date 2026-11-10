/**
 * 标签口径的纯函数：ID 归一化、标签名校验、大类推断与归类。
 *
 * 逐条对齐 `tools/template_catalog.py`（`TAG_CATEGORIES` / `validate_tag_name` /
 * `infer_tag_category` / `normalize_tag_categories`）与 `tools/template_store.py::normalize_id`。
 * 这里不放任何 IO，方便单测直接比对 Python 行为。
 */

/** 默认大类顺序；老文件没有 `category_order` 时以它为起点。 */
export const TAG_CATEGORIES: readonly string[] = ['景别', '适合季节', '场景', '姿势'];

/** 标签名不合法（对应 Python `CatalogError`）；调用方在对外接口里要转成 StoreError。 */
export class TagNameError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TagNameError';
    Object.setPrototypeOf(this, TagNameError.prototype);
  }
}

/**
 * 模板 ID 规范化：纯数字补零到 3 位，其余原样。
 *
 * 与 Python `str(value or "").strip()` 同义——`0` / `null` / `false` 都当作空串，
 * 避免 `Number(0)` 被补成 `"000"`。
 */
export function normalizeId(value: unknown): string {
  const text = rawText(value).trim();
  return /^\d+$/.test(text) ? text.padStart(3, '0') : text;
}

/** Python `str(value or "")` 的近似：假值一律空串，其余转字符串（不 trim）。 */
export function rawText(value: unknown): string {
  if (value === null || value === undefined || value === false || value === 0 || value === '') return '';
  return String(value);
}

/**
 * 标签名校验：空、超 64 字、含分隔符都拒绝。
 *
 * 64 字按码点算（Python `len()` 的口径），所以用 `[...name].length` 而不是 `name.length`，
 * 否则带 emoji 的标签会比 Python 早一位被判超长。
 */
export function validateTagName(value: unknown): string {
  const name = String(value ?? '').trim();
  if (!name) throw new TagNameError('标签名称不能为空');
  if ([...name].length > 64) throw new TagNameError('标签名称最多 64 个字符');
  if (/[|;\r\n]/.test(name)) throw new TagNameError('标签不能包含分隔符 |、; 或换行');
  return name;
}

/** 按标签词义猜大类；猜不到一律「场景」（与 Python 完全一致，别改顺序）。 */
export function inferTagCategory(tag: unknown): string {
  const name = String(tag ?? '').trim();
  if (['近景', '中景', '中远景', '远景', '特写'].includes(name)) return '景别';
  if (['适合春夏', '适合秋冬', '适合春季', '适合夏季', '适合秋季', '适合冬季'].includes(name)) return '适合季节';
  if (['手串展示', '只有上半身', '坐在椅子上', '裤子展示', '站立', '坐姿'].includes(name)) return '姿势';
  return '场景';
}

/**
 * 把每个标签归到某个大类。
 *
 * 已保存的分类只在 `allowed`（当前大类顺序表）里有效；否则退回推断值，推断值也不在
 * `allowed` 里时取第一个大类——保证前端拿到的每个标签都有分类可用。
 */
export function normalizeTagCategories(
  tags: readonly string[],
  categories?: Record<string, unknown> | null,
  allowed?: readonly string[] | null,
): Record<string, string> {
  const allowedList = (allowed && allowed.length ? allowed : TAG_CATEGORIES)
    .map((value) => String(value ?? '').trim())
    .filter(Boolean);
  const saved = categories ?? {};
  const result: Record<string, string> = {};
  for (const tag of tags) result[tag] = inferTagCategory(tag);
  for (const tag of tags) {
    const category = saved[tag];
    if (typeof category === 'string' && allowedList.includes(category)) {
      result[tag] = category;
    } else if (!allowedList.includes(result[tag])) {
      result[tag] = allowedList[0] ?? '场景';
    }
  }
  return result;
}
