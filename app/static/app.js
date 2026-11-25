document.addEventListener("submit", (event) => {
  const form = event.target;
  // 优先使用实际提交按钮的 data-confirm（包括键盘激活），再回退到表单。
  const message = event.submitter?.dataset.confirm ?? form.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-confirm]");
  if (link && !window.confirm(link.dataset.confirm)) event.preventDefault();
  if (event.target.closest('button[data-action="print"]')) window.print();
});
