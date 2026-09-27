const TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/original';

const slugInput = document.getElementById('slug-input');
const mediaRootSelect = document.getElementById('media-root-select');
const fetchDataBtn = document.getElementById('fetch-data-btn');
const jsonEditor = document.getElementById('json-editor');
const submitBtn = document.getElementById('submit-btn');
const generateTmdbBtn = document.getElementById('generate-tmdb-btn');
const autoFillSeasonsBtn = document.getElementById('auto-fill-seasons-btn');
const posterImg = document.getElementById('poster-preview');
const backdropImg = document.getElementById('backdrop-preview');
const fileBrowser = document.getElementById('file-browser-tree');

const POSTER_PLACEHOLDER = '/www/static/img/poster_null.png';
const BACKDROP_PLACEHOLDER = '/www/static/img/backdrop_null.png';

function formatFileSize(bytes) {
    if (bytes === null || bytes === undefined) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = bytes;
    let unitIndex = 0;
    while (size >= 1024 && unitIndex < units.length - 1) {
        size /= 1024;
        unitIndex++;
    }
    return `${size.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function currentSlug() {
    return slugInput.value.trim();
}

// Populates the media root dropdown from the server's configured
// media_dirs. Called once on page load. If there's only one root,
// the dropdown still shows it (so it's always explicit what gets used).
async function populateMediaRoots() {
    try {
        const res = await fetch('/api/create/media-roots');
        if (!res.ok) return;
        const data = await res.json();
        mediaRootSelect.innerHTML = '';
        for (const root of data.roots) {
            const opt = document.createElement('option');
            opt.value = root;
            opt.textContent = root;
            mediaRootSelect.appendChild(opt);
        }
    } catch (err) {
        // Leave the dropdown empty; save will fail with a clear error if used.
    }
}

function parseEditorJson() {
    try {
        return JSON.parse(jsonEditor.value || '{}');
    } catch (err) {
        return null;
    }
}

async function checkAssetExists(slug, assetPath) {
    if (!assetPath) return false;
    try {
        const res = await fetch(`/api/create/check-asset/${encodeURIComponent(slug)}/${assetPath}`);
        if (!res.ok) return false;
        const data = await res.json();
        return !!data.exists;
    } catch (err) {
        return false;
    }
}

async function fetchTmdbDefaults(mediaType, tmdbId) {
    try {
        const res = await fetch(`/api/create/tmdb-defaults/${mediaType}/${tmdbId}`);
        if (!res.ok) return null;
        return await res.json();
    } catch (err) {
        return null;
    }
}

// Resolves a single image (poster or backdrop) following the fallback chain,
// and sets it on the given <img> element.
async function resolveImage(imgEl, placeholder, slug, localPath, tmdbDefaultPath) {
    // 1. Local file on disk
    if (localPath) {
        const exists = await checkAssetExists(slug, localPath);
        if (exists) {
            imgEl.src = `/media-assets/${slug}/${localPath}`;
            return;
        }
        // 2. metadata.json had a path, but no local file -> use TMDB CDN directly
        imgEl.src = `${TMDB_IMAGE_BASE}${localPath}`;
        return;
    }
    // 3. No path in metadata.json at all -> use TMDB's default for this tmdb_id, if we have one
    if (tmdbDefaultPath) {
        imgEl.src = `${TMDB_IMAGE_BASE}${tmdbDefaultPath}`;
        return;
    }
    // 4. Nothing available
    imgEl.src = placeholder;
}

// Re-resolves both poster and backdrop previews based on the current slug
// input and JSON editor contents.
async function updatePreviews() {
    const slug = currentSlug();
    const data = parseEditorJson();

    if (!slug || !data) {
        posterImg.src = POSTER_PLACEHOLDER;
        backdropImg.src = BACKDROP_PLACEHOLDER;
        return;
    }

    let tmdbDefaults = null;
    const needsTmdbFallback = !data.poster_path || !data.backdrop_path;
    if (needsTmdbFallback && data.tmdb_id && (data.media_type === 'movie' || data.media_type === 'tv')) {
        tmdbDefaults = await fetchTmdbDefaults(data.media_type, data.tmdb_id);
    }

    await Promise.all([
        resolveImage(posterImg, POSTER_PLACEHOLDER, slug, data.poster_path, tmdbDefaults?.poster_path),
        resolveImage(backdropImg, BACKDROP_PLACEHOLDER, slug, data.backdrop_path, tmdbDefaults?.backdrop_path),
    ]);
}

// File Browser

async function browseDir(mediaRoot, relPath) {
    const url = `/api/create/browse?root=${encodeURIComponent(mediaRoot)}&path=${encodeURIComponent(relPath)}`;
    const res = await fetch(url);
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || res.statusText);
    }
    return res.json();
}

function closeAllMenus(exceptMenu) {
    document.querySelectorAll('.file-browser-menu.open').forEach(menu => {
        if (menu !== exceptMenu) menu.classList.remove('open');
    });
}

// Builds a single entry row (<li>), and for directories, a lazily-loaded
// nested <ul> that fills in the first time it's expanded.
function renderEntry(entry, mediaRoot) {
    const li = document.createElement('li');
    li.className = 'file-browser-entry';

    const row = document.createElement('div');
    row.className = 'file-browser-row';

    const nameSpan = document.createElement('span');
    nameSpan.className = entry.is_dir ? 'file-browser-name file-browser-dir' : 'file-browser-name file-browser-file';
    nameSpan.textContent = entry.is_dir ? `📁 ${entry.name}` : `📄 ${entry.name}`;
    row.appendChild(nameSpan);

    if (!entry.is_dir) {
        const sizeSpan = document.createElement('span');
        sizeSpan.className = 'file-browser-size';
        sizeSpan.textContent = formatFileSize(entry.size);
        row.appendChild(sizeSpan);
    }

    // Every row gets a path hint + copy affordance, since these rel_paths
    // are exactly what goes into metadata.json's episode_path/movie_path/
    // poster_path/backdrop_path fields.
    const menuWrap = document.createElement('div');
    menuWrap.className = 'file-browser-menu-wrap';
    const menuBtn = document.createElement('button');
    menuBtn.className = 'file-browser-menu-btn';
    menuBtn.textContent = '⋮';
    menuBtn.type = 'button';
    const menu = document.createElement('div');
    menu.className = 'file-browser-menu';

    const copyItem = document.createElement('button');
    copyItem.type = 'button';
    copyItem.textContent = 'Copy relative path';
    copyItem.addEventListener('click', () => {
        navigator.clipboard?.writeText(entry.rel_path);
        menu.classList.remove('open');
    });
    menu.appendChild(copyItem);

    let childUl = null;
    let expanded = false;
    if (entry.is_dir) {
        const browseItem = document.createElement('button');
        browseItem.type = 'button';
        browseItem.textContent = 'Browse';
        browseItem.addEventListener('click', () => {
            menu.classList.remove('open');
            toggleExpand();
        });
        menu.appendChild(browseItem);
    }

    menuBtn.addEventListener('click', (evt) => {
        evt.stopPropagation();
        const willOpen = !menu.classList.contains('open');
        closeAllMenus();
        if (willOpen) menu.classList.add('open');
    });

    menuWrap.appendChild(menuBtn);
    menuWrap.appendChild(menu);
    row.appendChild(menuWrap);
    li.appendChild(row);

    async function toggleExpand() {
        if (!entry.is_dir) return;
        if (expanded) {
            childUl?.remove();
            childUl = null;
            expanded = false;
            nameSpan.textContent = `📁 ${entry.name}`;
            return;
        }
        nameSpan.textContent = `📂 ${entry.name} (loading...)`;
        try {
            const data = await browseDir(mediaRoot, entry.rel_path);
            childUl = document.createElement('ul');
            childUl.className = 'file-browser-list file-browser-nested';
            if (data.entries.length === 0) {
                const emptyLi = document.createElement('li');
                emptyLi.className = 'file-browser-empty';
                emptyLi.textContent = '(empty)';
                childUl.appendChild(emptyLi);
            } else {
                for (const child of data.entries) {
                    childUl.appendChild(renderEntry(child, mediaRoot));
                }
            }
            li.appendChild(childUl);
            expanded = true;
            nameSpan.textContent = `📂 ${entry.name}`;
        } catch (err) {
            nameSpan.textContent = `📁 ${entry.name} (error: ${err.message})`;
        }
    }

    if (entry.is_dir) {
        nameSpan.style.cursor = 'pointer';
        nameSpan.addEventListener('click', toggleExpand);
    }

    return li;
}

async function refreshFileBrowser() {
    const mediaRoot = mediaRootSelect.value;
    if (!fileBrowser) return;
    if (!mediaRoot) {
        fileBrowser.innerHTML = '<li class="file-browser-empty">No media root selected.</li>';
        return;
    }
    fileBrowser.innerHTML = '<li class="file-browser-empty">Loading...</li>';
    try {
        const data = await browseDir(mediaRoot, '');
        fileBrowser.innerHTML = '';
        if (data.entries.length === 0) {
            fileBrowser.innerHTML = '<li class="file-browser-empty">(empty)</li>';
            return;
        }
        for (const entry of data.entries) {
            fileBrowser.appendChild(renderEntry(entry, mediaRoot));
        }
    } catch (err) {
        fileBrowser.innerHTML = `<li class="file-browser-empty">Failed to browse: ${err.message}</li>`;
    }
}

document.addEventListener('click', () => closeAllMenus());

// Event wiring

async function handleFetchData() {
    const slug = currentSlug();
    if (!slug) {
        alert('Enter a slug first.');
        return;
    }

    fetchDataBtn.disabled = true;
    fetchDataBtn.textContent = 'Loading...';
    try {
        const res = await fetch(`/api/create/load/${encodeURIComponent(slug)}`);
        if (res.status === 404) {
            const createRes = await fetch(`/api/create/save/${encodeURIComponent(slug)}?media_root=${encodeURIComponent(mediaRootSelect.value || '')}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            if (!createRes.ok) {
                const err = await createRes.json().catch(() => ({}));
                alert(`Failed to create blank metadata.json: ${err.detail || createRes.statusText}`);
                return;
            }
            jsonEditor.value = JSON.stringify({}, null, 2);
            alert(`Created blank metadata.json for slug "${slug}".`);
            return;
        }
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            alert(`Failed to load metadata: ${err.detail || res.statusText}`);
            return;
        }
        const data = await res.json();
        jsonEditor.value = JSON.stringify(data.metadata, null, 2);
        if (data.media_root) {
            mediaRootSelect.value = data.media_root;
        }
        await updatePreviews();
    } catch (err) {
        alert(`Failed to load metadata: ${err.message}`);
    } finally {
        fetchDataBtn.disabled = false;
        fetchDataBtn.textContent = 'Fetch Data';
    }
}

// "Upload metadata.json" button: saves the editor's contents back to disk
async function handleSubmit() {
    const slug = currentSlug();
    const data = parseEditorJson();
    if (!slug) {
        alert('Enter a slug first.');
        return;
    }
    if (!data) {
        alert('JSON editor contains invalid JSON.');
        return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = 'Saving...';
    try {
        const mediaRoot = mediaRootSelect.value;
        if (!mediaRoot) {
            alert('No media root selected.');
            return;
        }
        const res = await fetch(`/api/create/save/${encodeURIComponent(slug)}?media_root=${encodeURIComponent(mediaRoot)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            alert(`Failed to save metadata.json: ${err.detail || res.statusText}`);
            return;
        }
        alert('metadata.json saved.');
    } catch (err) {
        alert(`Failed to save metadata.json: ${err.message}`);
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Upload metadata.json';
    }
}

// "Generate tmdb_metadata.json" button: asks the backend to fetch full
// TMDB metadata for the slug's tmdb_id/media_type and write it to disk
async function handleGenerateTmdb() {
    const slug = currentSlug();
    if (!slug) {
        alert('Enter a slug first.');
        return;
    }

    generateTmdbBtn.disabled = true;
    generateTmdbBtn.textContent = 'Generating...';
    try {
        const res = await fetch(`/api/create/generate-tmdb/${encodeURIComponent(slug)}`, { method: 'POST' });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            alert(`Failed to generate tmdb_metadata.json: ${err.detail || res.statusText}`);
            return;
        }
        const result = await res.json();
        alert(`tmdb_metadata.json written to ${result.path}`);
        await updatePreviews();
    } catch (err) {
        alert(`Failed to generate tmdb_metadata.json: ${err.message}`);
    } finally {
        generateTmdbBtn.disabled = false;
        generateTmdbBtn.textContent = 'Generate tmdb_metadata.json';
    }
}

// "Auto-fill Seasons" button: scans the slug's subfolders on disk (each
// treated as a season, alphabetically) and each folder's video files
// (also alphabetically) as episodes, then REPLACES the seasons array in
// the JSON editor. seasons[0] is left as {} to match this project's
// existing convention of reserving index 0, with folders mapped starting
// at index 1.
async function handleAutoFillSeasons() {
    const slug = currentSlug();
    const mediaRoot = mediaRootSelect.value;
    if (!slug) {
        alert('Enter a slug first.');
        return;
    }
    if (!mediaRoot) {
        alert('No media root selected.');
        return;
    }
    const data = parseEditorJson();
    if (!data) {
        alert('JSON editor contains invalid JSON.');
        return;
    }

    autoFillSeasonsBtn.disabled = true;
    autoFillSeasonsBtn.textContent = 'Scanning...';
    try {
        const res = await fetch(`/api/create/scan-seasons/${encodeURIComponent(slug)}?media_root=${encodeURIComponent(mediaRoot)}`);
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            alert(`Failed to scan seasons: ${err.detail || res.statusText}`);
            return;
        }
        const result = await res.json();
        if (result.seasons.length === 0) {
            alert('No subfolders found under this slug - nothing to fill in.');
            return;
        }

        const newSeasons = [{}]; // index 0 reserved, matches existing metadata.json convention
        for (const season of result.seasons) {
            newSeasons.push({
                episodes: season.episode_paths.map(path => ({ episode_path: path })),
            });
        }

        data.seasons = newSeasons;
        jsonEditor.value = JSON.stringify(data, null, 2);
        await updatePreviews();

        const folderNames = result.seasons.map(s => s.folder_name).join(', ');
        alert(`Filled in ${result.seasons.length} season(s) from folders: ${folderNames}`);
    } catch (err) {
        alert(`Failed to scan seasons: ${err.message}`);
    } finally {
        autoFillSeasonsBtn.disabled = false;
        autoFillSeasonsBtn.textContent = 'Auto-fill Seasons';
    }
}

fetchDataBtn.addEventListener('click', handleFetchData);
submitBtn.addEventListener('click', handleSubmit);
generateTmdbBtn.addEventListener('click', handleGenerateTmdb);
autoFillSeasonsBtn.addEventListener('click', handleAutoFillSeasons);
mediaRootSelect.addEventListener('change', refreshFileBrowser);
// Re-resolve previews whenever the JSON editor is hand-edited, so pasting
// in a different poster_path etc. updates the preview without re-fetching.
jsonEditor.addEventListener('input', () => {
    clearTimeout(jsonEditor._debounce);
    jsonEditor._debounce = setTimeout(updatePreviews, 400);
});

(async function init() {
    await populateMediaRoots();
    await refreshFileBrowser();
})();