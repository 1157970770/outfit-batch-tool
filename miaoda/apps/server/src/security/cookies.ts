/** 与 Python `SimpleCookie` 等价的最小实现：只解析 Cookie 头，取第一个同名值。 */
export function parseCookies(header: string | undefined): Record<string, string> {
  const jar: Record<string, string> = {};
  if (!header) return jar;
  for (const part of header.split(';')) {
    const index = part.indexOf('=');
    if (index <= 0) continue;
    const name = part.slice(0, index).trim();
    const value = part.slice(index + 1).trim();
    if (!name) continue;
    if (!(name in jar)) jar[name] = decodeURIComponent(value);
  }
  return jar;
}
