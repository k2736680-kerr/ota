async function loadReports() {
  const data = await api("/api/reports");
  const body = document.querySelector("#report-body");
  const items = data.reports || [];
  if (!items.length) {
    body.innerHTML = "<tr><td colspan='3'>还没有报告</td></tr>";
    return;
  }
  body.innerHTML = items.map((item) => `
    <tr>
      <td>${item.slug}</td>
      <td><a href="${item.html}" target="_blank">预览</a></td>
      <td><a href="${item.csv}">下载 CSV</a></td>
    </tr>`).join("");
}

async function showDirs() {
  const data = await api("/api/system/dirs");
  document.querySelector("#dirs").textContent =
    `logs：${data.logs}\nevidence：${data.evidence}\nreports：${data.reports}`;
}
