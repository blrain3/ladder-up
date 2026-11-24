document.querySelector("#assessment-score-form").addEventListener("formdata", (event) => {
    const data = event.formData;
    const memberIds = data.getAll("member_ids");
    data.delete("member_ids");
    for (const id of memberIds) {
        if (data.get(`solved_${id}`) !== data.get(`old_solved_${id}`) ||
            data.get(`score_${id}`) !== data.get(`old_score_${id}`)) {
            data.append("member_ids", id);
            continue;
        }
        for (const field of ["solved", "score", "version", "old_solved", "old_score"]) {
            data.delete(`${field}_${id}`);
        }
    }
});
