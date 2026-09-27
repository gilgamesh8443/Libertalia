(function () {
    const seasonButtons = document.querySelectorAll('.season-button');
    const seasonSelect = document.getElementById('season-select');
    const episodeLists = document.querySelectorAll('[data-season-episodes]');

    function selectSeason(season) {
        seasonButtons.forEach((b) => b.classList.toggle('active', b.dataset.season === season));
        episodeLists.forEach((list) => {
            list.hidden = list.dataset.seasonEpisodes !== season;
        });

        const url = new URL(window.location);
        url.searchParams.set('season', season);
        history.pushState({}, '', url);
    }

    seasonButtons.forEach((btn) => {
        btn.addEventListener('click', () => selectSeason(btn.dataset.season));
    });

    if (seasonSelect) {
        seasonSelect.addEventListener('change', () => selectSeason(seasonSelect.value));
    }
})();