/** 款号标签：上衣必填（云端归档路径按它命名），其余选填。 */
export const STYLE_TAG_FIELDS: Array<{ key: string; label: string; placeholder: string; maxLength: number }> = [
  { key: 'top', label: '上衣', placeholder: '必填，如 AX111', maxLength: 40 },
  { key: 'lining', label: '内衬', placeholder: '选填', maxLength: 40 },
  { key: 'pants', label: '裤子', placeholder: '选填', maxLength: 40 },
  { key: 'shoes', label: '鞋子', placeholder: '选填', maxLength: 40 },
  { key: 'scene', label: '场景', placeholder: '选填，如 秋冬', maxLength: 40 },
  { key: 'note', label: '特殊备注', placeholder: '选填', maxLength: 100 },
];

interface Props {
  value: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}

export function StyleTags({ value, onChange }: Props) {
  return (
    <div className="tag-input-grid">
      {STYLE_TAG_FIELDS.map((field) => (
        <label key={field.key}>
          {field.label}
          <input
            value={value[field.key] || ''}
            maxLength={field.maxLength}
            autoComplete="off"
            placeholder={field.placeholder}
            onChange={(event) => onChange({ ...value, [field.key]: event.target.value })}
          />
        </label>
      ))}
    </div>
  );
}
