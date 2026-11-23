const CHUNK_BYTES = 96 * 1024;

function chunkText(buffer) {
  const bytes = new Uint8Array(buffer);
  let text = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    text += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  }
  return btoa(text);
}

export async function uploadSelectedFile(invoke, message, file, progress) {
  const call = (body) => invoke("selectedAttachment", body);
  const start = await call({
    action: "begin", accountId: message.accountId, conversationId: message.conversationId,
    messageId: message.messageId, fileName: file.name, sizeBytes: file.size,
    mimeType: file.type || "application/octet-stream",
  });
  const ticket = start.ticket;
  if (!ticket || start.messageId !== message.messageId) throw new Error("文件选择回执不匹配");
  let finished = false;
  try {
    for (let offset = 0; offset < file.size; offset += CHUNK_BYTES) {
      const buffer = await file.slice(offset, offset + CHUNK_BYTES).arrayBuffer();
      const reply = await call({ action: "chunk", ticket, offset, bytes: chunkText(buffer) });
      if (reply.ticket !== ticket || reply.received !== offset + buffer.byteLength) {
        throw new Error("文件传输回执不匹配");
      }
      progress(`正在准备原件 ${Math.floor(reply.received * 100 / file.size)}%`);
    }
    let result = await call({ action: "finish", ticket });
    finished = true;
    progress("正在上传，等待文件回执");
    while (result.state === "uploading") {
      await new Promise((resolve) => setTimeout(resolve, 750));
      result = await call({ action: "status", ticket });
    }
    if (result.ticket !== ticket || result.messageId !== message.messageId ||
        result.state !== "complete" || !result.fileId) {
      throw new Error(result.error || "未取得对应原件的完成回执");
    }
    return result;
  } finally {
    if (!finished) await call({ action: "cancel", ticket }).catch(() => {});
  }
}

export function selectedUploadControl(message, invoke) {
  const label = document.createElement("label");
  label.className = "selected-upload";
  const title = document.createElement("span");
  title.textContent = "选择导出原件";
  const picker = document.createElement("input");
  picker.type = "file";
  if (message.messageType === "image") picker.accept = "image/*";
  if (message.messageType === "voice") picker.accept = "audio/*";
  const status = document.createElement("output");
  status.setAttribute("aria-live", "polite");
  picker.addEventListener("change", async () => {
    const file = picker.files?.[0];
    if (!file) return;
    picker.disabled = true;
    const progress = (text) => { status.textContent = text; };
    try {
      progress("正在核对原消息");
      const result = await uploadSelectedFile(invoke, message, file, progress);
      progress(`消息 ID ${result.messageId} · fileId ${result.fileId}`);
    } catch (error) {
      progress(error instanceof Error ? error.message : "原件上传失败");
    } finally {
      picker.value = "";
      picker.disabled = false;
    }
  });
  label.append(title, picker, status);
  return label;
}
