const notice = document.querySelector("#polling-notice");

async function pollJob() {
    try {
        const response = await fetch(location.pathname, {headers: {"Accept": "application/json"}});
        if (!response.ok) {
            throw new Error("无法查询作业");
        }
        const {data} = await response.json();
        if (data.status !== "running") {
            location.reload();
            return;
        }
        setTimeout(pollJob, 2000);
    } catch {
        notice.firstChild.textContent = "作业状态查询失败。";
    }
}

setTimeout(pollJob, 2000);
