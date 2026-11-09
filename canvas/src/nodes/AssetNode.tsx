import { useEffect, useRef, useState } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { IconBookmarkPlus, IconPhoto, IconPhotoPlus, IconReplace } from '@tabler/icons-react'
import { errorMessage, uploadImage } from '../api'
import { NodeHead } from '../NodeHead'
import { toast } from '../run'
import { useCanvasStore } from '../store'
import type { AssetNodeType } from '../types'

export function AssetNode({ id, data, selected }: NodeProps<AssetNodeType>) {
  const update = useCanvasStore((state) => state.updateNodeData)
  const inputRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [drag, setDrag] = useState(false)

  async function handleFile(file: File | undefined | null) {
    if (!file) return
    if (!file.type.startsWith('image/')) {
      update(id, { error: '请选择 JPG / PNG / WebP / BMP 图片。' })
      return
    }
    setBusy(true)
    update(id, { error: '' })
    try {
      const result = await uploadImage(file)
      update(id, {
        uploadId: result.upload_id,
        name: result.name,
        url: `/api/uploads/${result.upload_id}`,
        width: result.width ?? null,
        height: result.height ?? null,
        error: '',
      })
    } catch (error) {
      update(id, { error: errorMessage(error) })
    } finally {
      setBusy(false)
    }
  }

  const hasImage = Boolean(data.url)

  function saveMaterial() {
    if (!data.uploadId || !data.url) return
    const added = useCanvasStore.getState().saveLibraryItem({
      uploadId: data.uploadId,
      name: data.name || '素材',
      url: data.url,
      width: data.width ?? null,
      height: data.height ?? null,
    })
    toast(added ? '已存入素材库' : '这张图已经在素材库里了')
  }

  // 从右键菜单「上传图片」新建时顺手拉起文件选择；autoPick 只生效一次。
  useEffect(() => {
    if (!data.autoPick) return
    update(id, { autoPick: false })
    inputRef.current?.click()
  }, [data.autoPick, id, update])

  return (
    <div className={`cv-node cv-node--asset${selected ? ' is-selected' : ''}`}>
      <NodeHead nodeId={id} icon={<IconPhoto size={15} stroke={1.8} />} title='图片素材' kind='Asset' />
      <div
        className={`cv-node-body cv-asset-body${drag ? ' is-drag' : ''}`}
        onDragOver={(event) => {
          event.preventDefault()
          event.stopPropagation()
          setDrag(true)
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(event) => {
          event.preventDefault()
          event.stopPropagation()
          setDrag(false)
          void handleFile(event.dataTransfer.files?.[0])
        }}
      >
        {hasImage ? (
          <div className='cv-asset-preview'>
            <img src={data.url} alt={data.name || '参考图'} loading='lazy' />
            {busy ? <span className='cv-asset-busy'>上传中…</span> : null}
          </div>
        ) : (
          <button
            type='button'
            className='cv-asset-drop nodrag'
            disabled={busy}
            onClick={() => inputRef.current?.click()}
          >
            <IconPhotoPlus size={26} stroke={1.5} />
            <strong>{busy ? '上传中…' : drag ? '松开即可上传' : '点击或拖入图片'}</strong>
            <span>JPG / PNG / WebP / BMP，单张 ≤20MB</span>
          </button>
        )}
        <input
          ref={inputRef}
          type='file'
          accept='.jpg,.jpeg,.png,.webp,.bmp'
          hidden
          onChange={(event) => {
            void handleFile(event.target.files?.[0])
            event.target.value = ''
          }}
        />
        {data.error ? <p className='cv-note is-error'>{data.error}</p> : null}
      </div>
      <div className='cv-node-foot'>
        <span
          className='cv-ellipsis'
          title={data.width && data.height ? `${data.width}×${data.height}` : undefined}
        >
          {data.name || '未选择图片'}
        </span>
        {hasImage ? (
          <button type='button' className='cv-ghost nodrag' title='保存到素材库' onClick={saveMaterial}>
            <IconBookmarkPlus size={13} /> 存素材
          </button>
        ) : null}
        {hasImage ? (
          <button type='button' className='cv-ghost nodrag' onClick={() => inputRef.current?.click()}>
            <IconReplace size={13} /> 更换
          </button>
        ) : null}
      </div>
      <Handle
        type='source'
        position={Position.Right}
        className='cv-handle'
        isConnectable={Boolean(data.uploadId)}
      />
    </div>
  )
}
