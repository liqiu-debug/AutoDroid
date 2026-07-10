/** 从 axios 错误对象中提取可读的错误信息。 */
export const getErrorDetail = (err, fallback = '操作失败') => {
  return err?.response?.data?.detail || err?.message || fallback
}
