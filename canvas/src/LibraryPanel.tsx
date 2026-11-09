import { useEffect, useState } from 'react'
import { IconBookmark, IconPencil, IconPlus, IconTrash, IconX } from '@tabler/icons-react'
import type { LibraryItem } from './persist'
import { useCanvasStore } from './store'

type Props = {
  open: boolean
  onClose: () => void
  onPlace: (item: LibraryItem) => void
}

export function LibraryPanel({ open, onClose, onPlace }: Props) {
  const library = useCanvasStore((state) => state.library)
  const rename = useCanvasStore((state) => state.renameLibraryItem)
  const remove = useCanvasStore((state) => state.removeLibraryItem)
  const [editing, setEditing] = useState<{ id: string; value: string } | null>(null)
  if (!open) return null

  function commitRename() {
    if (!editing) return
    rename(editing.id, editing.value)
    setEditing(null)
  }

  // onBlur 之外再兜一层：点画布其它地方也能落名（部分环境 blur 不触发）。
  useEffect(() => {
    if (!editing) return
    const onPointerDown = (event: Event) => {
      const target = event.target as Element | null
      if (target?.closest?.('.cv-library-input')) return
      commitRename()
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => document.removeEventListener('pointerdown', onPointerDown, true)
  }, [editing])

  return (
    <aside className='cv-library' aria-label='素材库'>
      <header className='cv-library-head'>
        <IconBookmark size={15} stroke={1.8} />
        <strong>素材库</strong>
        <span className='cv-library-count'>{library.length}</span>
        <button type='button' className='cv-icon-btn' title='收起素材库' onClick={onClose}>
          <IconX size={14} />
        </button>
      </header>

      {library.length ? (
        <div className='cv-library-grid'>
          {library.map((item) => (
            <figure key={item.id} className='cv-library-item'>
              <button
                type='button'
                className='cv-library-thumb'
                title={`添加到画布：${item.name || '图片'}`}
                onClick={() => onPlace(item)}
              >
                <img src={item.url} alt={item.name || '素材'} loading='lazy' />
                <span className='cv-library-add' aria-hidden='true'>
                  <IconPlus size={14} />
                </span>
              </button>
              <figcaption title={editing?.id === item.id ? undefined : item.name}>
                {editing?.id === item.id ? (
                  <input
                    className='cv-library-input'
                    value={editing.value}
                    maxLength={60}
                    autoFocus
                    onChange={(event) => setEditing({ id: item.id, value: event.target.value })}
                    onBlur={commitRename}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') {
                        event.preventDefault()
                        commitRename()
                      } else if (event.key === 'Escape') {
                        event.preventDefault()
                        setEditing(null)
                      }
                    }}
                  />
                ) : (
                  <>
                    <span
                      className='cv-ellipsis'
                      title='双击重命名'
                      onDoubleClick={() => setEditing({ id: item.id, value: item.name })}
                    >
                      {item.name || '图片'}
                    </span>
                    <button
                      type='button'
                      className='cv-library-rename'
                      title='重命名'
                      onClick={() => setEditing({ id: item.id, value: item.name })}
                    >
                      <IconPencil size={12} />
                    </button>
                    <button
                      type='button'
                      className='cv-library-del'
                      title='从素材库移除（不影响画布里的图片）'
                      onClick={() => remove(item.id)}
                    >
                      <IconTrash size={12} />
                    </button>
                  </>
                )}
              </figcaption>
            </figure>
          ))}
        </div>
      ) : (
        <p className='cv-library-empty'>
          点一下图片节点里的「存素材」，图片就会收到这里，之后随时点开复用。
        </p>
      )}
    </aside>
  )
}
