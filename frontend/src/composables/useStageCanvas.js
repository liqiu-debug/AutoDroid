import { ref } from 'vue'
import { findBestNodeAtPoint, findBestRecordableNode } from '@/utils/recordableNode'

/**
 * 静态截图画布：坐标换算与元素悬停高亮。
 * - 屏幕坐标 <-> 截图真实坐标映射
 * - 根据坐标命中可录制节点 / 任意节点
 * - 悬停高亮框样式
 */
export function useStageCanvas({ liveMode, nodes, liveNodes }) {
  const canvasRef = ref(null)
  const imgRef = ref(null)
  const hoveredNode = ref(null)

  const getCurrentNodeList = () => (liveMode.value ? liveNodes.value : nodes.value)

  // Get mapped coordinates
  const getMappedCoordinates = (clientX, clientY) => {
    if (!imgRef.value || !canvasRef.value) return null

    const rect = imgRef.value.getBoundingClientRect()
    const x = clientX - rect.left
    const y = clientY - rect.top

    const scaleX = imgRef.value.naturalWidth / rect.width
    const scaleY = imgRef.value.naturalHeight / rect.height

    // Check if click is within image bounds
    if (x < 0 || x > rect.width || y < 0 || y > rect.height) {
      return null
    }

    return {
      canvasX: x,
      canvasY: y,
      realX: Math.round(x * scaleX),
      realY: Math.round(y * scaleY),
      scaleX,
      scaleY,
      rect
    }
  }

  // Find node at coordinates
  const findNodeAt = (realX, realY) => {
    return findBestRecordableNode(getCurrentNodeList(), realX, realY)
  }

  const getFallbackNodeAt = (nodeList, realX, realY) => {
    return findBestNodeAtPoint(nodeList, realX, realY)
  }

  const onCanvasMouseLeave = () => {
    hoveredNode.value = null
  }

  // Get overlay style for hovered node
  const getOverlayStyle = () => {
    if (!hoveredNode.value || !imgRef.value) return { display: 'none' }

    const rect = imgRef.value.getBoundingClientRect()
    const canvasRect = canvasRef.value.getBoundingClientRect()

    const scaleX = rect.width / imgRef.value.naturalWidth
    const scaleY = rect.height / imgRef.value.naturalHeight

    const offsetX = rect.left - canvasRect.left
    const offsetY = rect.top - canvasRect.top

    const node = hoveredNode.value
    return {
      display: 'block',
      position: 'absolute',
      left: `${offsetX + node.x1 * scaleX}px`,
      top: `${offsetY + node.y1 * scaleY}px`,
      width: `${(node.x2 - node.x1) * scaleX}px`,
      height: `${(node.y2 - node.y1) * scaleY}px`,
      border: '2px solid #e74c3c',
      backgroundColor: 'rgba(231, 76, 60, 0.15)',
      pointerEvents: 'none',
      boxSizing: 'border-box',
      borderRadius: '4px'
    }
  }

  return {
    canvasRef,
    imgRef,
    hoveredNode,
    getCurrentNodeList,
    getMappedCoordinates,
    findNodeAt,
    getFallbackNodeAt,
    onCanvasMouseLeave,
    getOverlayStyle
  }
}
