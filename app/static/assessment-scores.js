document.querySelector("#assessment-score-form").addEventListener("formdata", (event) => {
    const data = event.formData;
    const memberIds = data.getAll("member_ids");
    data.delete("member_ids");
    for (const id of memberIds) {
        if (data.get(`solved_${id}`) !== data.get(`old_solved_${id}`) ||
            data.get(`score_${id}`) !== data.get(`old_score_${id}`) ||
            data.get(`kp_snapshot_${id}`) !== data.get(`old_kp_snapshot_${id}`)) {
            data.append("member_ids", id);
            continue;
        }
        for (const field of ["solved", "score", "kp_snapshot", "version", "old_solved", "old_score", "old_kp_snapshot"]) {
            data.delete(`${field}_${id}`);
        }
    }
});
