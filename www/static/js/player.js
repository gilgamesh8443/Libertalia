// Custom video controls to make everything look fancy

(function () {
    const stage = document.getElementById("player-stage");
    const overlay = document.getElementById("player-overlay");
    const video = document.getElementById("player-video");
    const controlsBar = document.getElementById("controls-bar");

    const isMobile = document.body.classList.contains("mobile");  // Some stuff changes on mobile

    if (!stage || !overlay || !video || !controlsBar) return;

    const playPauseBtn = document.getElementById("play-pause-btn");
    const scrubber = document.getElementById("scrubber");
    const currentTimeEl = document.getElementById("current-time");
    const durationTimeEl = document.getElementById("duration-time");
    const muteBtn = document.getElementById("mute-btn");
    const volumeSlider = document.getElementById("volume-slider");
    const fullscreenBtn = document.getElementById("fullscreen-btn");
    const settingsBtn = document.getElementById("player-settings-btn");
    const settingsPanel = document.getElementById("player-settings-panel");

    const HIDE_DELAY_MS = 3000;
    let hideTimer = null;
    let isScrubbing = false;

    // Transcoded streams (HEVC video or EAC3/AC3 audio in the source)
    // are re-encoded live and sent with no Content-Length or byte-range
    // support (see hub.py's /stream), so video.currentTime = X can't
    // work the normal way - there's no "byte offset" to jump to, only
    // an ffmpeg process that would need to restart from a new -ss.
    // isTranscoded mirrors that server-side decision via a data
    // attribute set in player.html, and knownDuration/seekBaseTime
    // let us fake a working duration/currentTime for the UI even
    // though the browser thinks each transcoded segment starts at 0.
    const isTranscoded = video.dataset.transcoded === "true";
    const knownDuration = Number(video.dataset.duration) || 0;
    let seekBaseTime = 0; // Seconds into the source that this <video> load's t=0 corresponds to

    // Show the real duration immediately, synchronously, using the server-provided
    // data-duration value - don't wait for the "loadedmetadata" event to do this.
    // This used to be set only inside a "loadedmetadata" listener, which meant the
    // scrubber/clock sat at 0:00 until the browser actually finished reading the
    // video's metadata over the network - slow or highly variable for transcoded/
    // remux streams specifically (fragmented MP4 with no leading moov atom), which
    // is exactly the "duration doesn't show up fast enough" symptom this fixes.
    // knownDuration is plain data baked into the HTML at page-render time (main.py's
    // cached ffprobe duration - see get_cached_duration), so there's nothing to wait
    // for here at all when it's present.
    if (knownDuration > 0) {
        scrubber.max = knownDuration;
        durationTimeEl.textContent = formatTime(knownDuration);
    }

    // True once the user switches to a non-zero audio track on a non-transcoded file.
    // The server then pipes a fragmented MP4 (same format as the transcode path), which
    // means video.duration is unreliable and byte-range seeking is gone — we fall back
    // to the same ?t= mechanism used by transcoded streams. Stays true for the rest of
    // the session (including if the user switches back to track 0), because once we've
    // used ?t= for a seek we can't go back to byte offsets without a full page reload.
    let isRemuxMode = false;

    // Formatting
    function formatTime(seconds) {
        if (!isFinite(seconds) || seconds < 0) return "0:00";
        const total = Math.floor(seconds);
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
        const ss = String(s).padStart(2, "0");
        return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    }

    // Icon state helper
    function showIcon(button, name) {
        button.querySelectorAll(".icon-state").forEach((el) => {
            el.hidden = el.dataset.icon !== name;
        });
    }

    // Play / pause
    function setPlayPauseIcon(playing) {
        showIcon(playPauseBtn, playing ? "pause" : "play");
        playPauseBtn.setAttribute("aria-label", playing ? "Pause" : "Play");
    }

    function togglePlayPause() {
        if (video.paused) {
            video.play();
        } else {
            video.pause();
        }
    }

    playPauseBtn.addEventListener("click", togglePlayPause);
    video.addEventListener("play", () => setPlayPauseIcon(true));
    video.addEventListener("pause", () => setPlayPauseIcon(false));

    // Click anywhere on the stage to toggle play/pause, except when clicking on the overlay or controls themselves
    stage.addEventListener("click", (e) => {
        if (
            e.target.closest(".player-overlay") ||
            e.target.closest(".controls-bar") ||
            e.target.closest(".player-settings-panel")
        ) {
            return;
        }
        if (isMobile) {
            // On mobile, tapping the stage toggles the UI instead of play/pause —
            // play/pause is handled by the dedicated button
            const isHidden = overlay.classList.contains("hidden");
            if (isHidden) {
                showControls();
            } else {
                clearTimeout(hideTimer);
                overlay.classList.add("hidden");
                controlsBar.classList.add("hidden");
                stage.classList.add("cursor-hidden");
            }
        } else {
            togglePlayPause();
        }
    });

    // Suppress the native right-click menu on the video
    video.addEventListener("contextmenu", (e) => e.preventDefault());

    // Scrubbing
    function updateScrubberFill() {
        const max = Number(scrubber.max) || 1; // Avoid divide-by-zero before duration is known
        const pct = (Number(scrubber.value) / max) * 100;
        scrubber.style.setProperty("--progress", `${pct}%`);
    }

    // Applies a duration to the scrubber/clock, but only once we have a value that's
    // actually usable (not 0, not NaN, not Infinity). Called from several events below
    // because no single browser event reliably gives us a good duration on every file:
    // loadedmetadata's video.duration can still be Infinity/NaN for some non-transcoded
    // fragmented files, and knownDuration can be 0 if get_cached_duration() missed the
    // ffprobe cache at page-render time (see main.py). Re-running this on every
    // candidate event means the scrubber self-heals as soon as any source of truth
    // becomes available, instead of getting stuck at 0:00 forever because the one
    // event we happened to listen to fired with a bad value.
    let durationResolved = false;
    function tryResolveDuration() {
        if (durationResolved) return;
        // Transcoded and remux streams both emit fragmented MP4 with no leading moov
        // atom, so video.duration is unreliable (often Infinity) until most of the
        // stream has arrived. The real duration is probed server-side (ffprobe) and
        // passed down via data-duration, so trust that for both modes - and don't
        // fall through to video.duration for them even if knownDuration is 0, since
        // that duration would be bogus anyway.
        let duration;
        if (isTranscoded || isRemuxMode) {
            duration = knownDuration;
        } else {
            duration = isFinite(video.duration) && video.duration > 0
                ? video.duration
                : knownDuration;
        }
        if (!isFinite(duration) || duration <= 0) return; // Still nothing usable - try again next event
        durationResolved = true;
        scrubber.max = duration;
        durationTimeEl.textContent = formatTime(duration);
        updateScrubberFill();
    }

    video.addEventListener("loadedmetadata", tryResolveDuration);
    video.addEventListener("durationchange", tryResolveDuration);
    video.addEventListener("canplay", tryResolveDuration);

    video.addEventListener("timeupdate", () => {
        // Cheap safety net: if every earlier event gave us a bad duration (e.g. the
        // stream took longer than usual to reveal a real moov atom), keep checking
        // on every timeupdate tick until one finally sticks.
        if (!durationResolved) tryResolveDuration();
        if (isScrubbing) return; // Don't fight the user's drag
        // seekBaseTime is 0 for normal playback and non-zero right
        // after a transcoded seek, where the new <video> load starts
        // counting from 0 again even though it represents a point
        // partway through the source.
        const displayTime = seekBaseTime + video.currentTime;
        scrubber.value = displayTime;
        currentTimeEl.textContent = formatTime(displayTime);
        updateScrubberFill();
    });

    scrubber.addEventListener("input", () => {
        isScrubbing = true;
        currentTimeEl.textContent = formatTime(Number(scrubber.value));
        updateScrubberFill();
    });

    scrubber.addEventListener("change", () => {
        const target = Number(scrubber.value);
        if (isTranscoded || isRemuxMode) {
            seekToTranscodedTime(target);
        } else {
            video.currentTime = target;
        }
        isScrubbing = false;
    });

    // Reloads the <video> element pointed at /stream?...&t=<seconds>,
    // which tells hub.py to restart ffmpeg with -ss at that timestamp.
    function seekToTranscodedTime(targetSeconds) {
        const source = document.getElementById("player-source");
        const wasPlaying = !video.paused;

        const url = new URL(source.src, window.location.href);
        url.searchParams.set("t", targetSeconds.toFixed(2));
        source.src = url.toString();
        seekBaseTime = targetSeconds;

        video.load();
        video.addEventListener("loadedmetadata", function resume() {
            if (wasPlaying) video.play();
            video.removeEventListener("loadedmetadata", resume);
        });
    }

    // Volume
    function setVolumeIcon() {
        let state;
        if (video.muted || video.volume === 0) {
            state = "volume-muted";
        } else if (video.volume < (1/3)) {
            state = "volume-off";
        } else if (video.volume < (2/3)) {
            state = "volume-low";
        } else {
            state = "volume-high";
        }
        showIcon(muteBtn, state);
        muteBtn.setAttribute("aria-label", state === "volume-muted" ? "Unmute" : "Mute");
    }

    muteBtn.addEventListener("click", () => {
        video.muted = !video.muted;
        setVolumeIcon();
    });

    volumeSlider.addEventListener("input", () => {
        video.volume = Number(volumeSlider.value);
        video.muted = video.volume === 0;
        setVolumeIcon();
    });

    // Fullscreen
    function isNativeFullscreen() {
        return (
            Math.abs(window.outerWidth - window.screen.width) <= 2 &&
            Math.abs(window.outerHeight - window.screen.height) <= 2
        );
    }

    function setFullscreenIcon() {
        const isFullscreen = !!document.fullscreenElement;
        showIcon(fullscreenBtn, isFullscreen ? "fullscreen-exit" : "fullscreen-enter");
        fullscreenBtn.setAttribute("aria-label", isFullscreen ? "Exit fullscreen" : "Fullscreen");
    }

    fullscreenBtn.addEventListener("click", () => {
        if (document.fullscreenElement) {
            document.exitFullscreen();
        } else {
            stage.requestFullscreen();
        }
    });

    // Covers Escape-key exit and any other non-button-driven change too
    document.addEventListener("fullscreenchange", setFullscreenIcon);

    // Covers F11 native fullscreen, which fires no fullscreenchange event
    window.addEventListener("resize", setFullscreenIcon);

    // Keyboard shortcuts (also covers most TV remote D-pad/OK/back mappings)
    document.addEventListener("keydown", (e) => {
        switch (e.key) {
            case " ":
            case "Enter":
                if (e.target === scrubber) return; // Let the range input handle Enter/space itself
                e.preventDefault();
                togglePlayPause();
                break;
            case "ArrowLeft":
                video.currentTime = Math.max(0, video.currentTime - 10);
                break;
            case "ArrowRight":
                video.currentTime = Math.min(video.duration || Infinity, video.currentTime + 10);
                break;
            case "ArrowUp":
                // Increase volume
                video.volume = Math.min(1, video.volume + 0.1);
                volumeSlider.value = video.volume;
                break;
            case "ArrowDown":
                video.volume = Math.max(0, video.volume - 0.1);
                volumeSlider.value = video.volume;
                break;
            case "f":
            case "F":
                fullscreenBtn.click();
                break;
        }
        showControls();
    });

    // Settings panel

    let isSettingsOpen = false;

    function openSettings() {
        isSettingsOpen = true;
        settingsPanel.hidden = false;
        settingsBtn.setAttribute("aria-expanded", "true");
        clearTimeout(hideTimer); // Don't let the panel get hidden while open
    }

    function closeSettings() {
        isSettingsOpen = false;
        settingsPanel.hidden = true;
        settingsBtn.setAttribute("aria-expanded", "false");
        scheduleHide();
    }

    settingsBtn.addEventListener("click", (e) => {
        e.stopPropagation(); // Don't let this bubble to the stage click-to-pause handler
        if (isSettingsOpen) {
            closeSettings();
        } else {
            openSettings();
        }
    });

    // Clicking outside the panel (but still within the stage) closes it,
    // without also triggering the stage's click-to-pause behavior.
    stage.addEventListener("click", (e) => {
        if (isSettingsOpen && !e.target.closest(".player-settings-panel") && !e.target.closest("#player-settings-btn")) {
            closeSettings();
        }
    });

    // Settings tabs

    const tabs = settingsPanel.querySelectorAll(".player-settings-tab");
    const sections = settingsPanel.querySelectorAll(".player-settings-section");

    tabs.forEach((tab) => {
        tab.addEventListener("click", () => {
            tabs.forEach((t) => {
                t.classList.toggle("active", t === tab);
                t.setAttribute("aria-selected", t === tab ? "true" : "false");
            });
            sections.forEach((s) => {
                s.hidden = s.dataset.section !== tab.dataset.tab;
            });
        });
    });

    // Track lists

    function renderTrackList(container, tracks, activeIndex, onSelect) {
        container.innerHTML = "";
        if (tracks.length === 0) {
            const empty = document.createElement("div");
            empty.className = "player-settings-track-option";
            empty.textContent = "No tracks found";
            container.appendChild(empty);
            return;
        }
        tracks.forEach((label, i) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "player-settings-track-option" + (i === activeIndex ? " selected" : "");
            btn.textContent = label;
            btn.addEventListener("click", () => onSelect(i));
            container.appendChild(btn);
        });
    }

    function refreshSubtitleTracks() {
        const container = document.getElementById("subtitle-track-list");
        const tracks = Array.from(video.textTracks || []);
        const labels = ["Off", ...tracks.map((t, i) => t.label || `Track ${i + 1}`)];
        const activeIndex = tracks.findIndex((t) => t.mode === "showing");
        renderTrackList(container, labels, activeIndex + 1, (selected) => {
            tracks.forEach((t, i) => {
                t.mode = i === selected - 1 ? "showing" : "hidden";
            });
            refreshSubtitleTracks();
        });
    }

    // Audio tracks (kinda weird cause browsers can't switch them natively)
    let currentAudioIndex = 0;

    function getAudioTracks() {
        try {
            return JSON.parse(video.dataset.audioTracks || "[]");
        } catch {
            return [];
        }
    }

    function switchAudioTrack(index) {
        if (index === currentAudioIndex) return;

        const wasPlaying = !video.paused;
        // For transcoded and remux streams, currentTime has reset to 0 on every
        // reload, so the real wall-clock position is seekBaseTime + currentTime.
        // For a plain static file (first load, no track switch yet) currentTime
        // is the real position and seekBaseTime is 0.
        const currentTime = (isTranscoded || isRemuxMode)
            ? seekBaseTime + video.currentTime
            : video.currentTime;

        // Once any audio switch happens on a non-transcoded file, the server starts
        // piping fragmented MP4 (stream-copy remux). Byte-range seeking is gone for
        // the rest of this session, so lock into ?t= mode. This stays true even if
        // the user switches back to track 0 — the server handles ?t= on track 0 fine.
        if (!isTranscoded) {
            isRemuxMode = true;
        }

        const source = document.getElementById("player-source");
        const container = document.getElementById("audio-track-list");

        container.classList.add("loading");

        const url = new URL(source.src, window.location.href);
        url.searchParams.set("audio", index);
        if (isTranscoded || isRemuxMode) {
            url.searchParams.set("t", currentTime.toFixed(2));
            seekBaseTime = currentTime;
        }
        source.src = url.toString();
        video.load();

        video.addEventListener("loadedmetadata", function resume() {
            if (!(isTranscoded || isRemuxMode)) video.currentTime = currentTime;
            if (wasPlaying) video.play();
            container.classList.remove("loading");
            video.removeEventListener("loadedmetadata", resume);
        });

        currentAudioIndex = index;
        refreshAudioTracks();
    }

    function refreshAudioTracks() {
        const container = document.getElementById("audio-track-list");
        const tracks = getAudioTracks();
        const labels = tracks.map((t, i) => t.title || (t.language ? t.language.toUpperCase() : `Track ${i + 1}`));
        renderTrackList(container, labels, currentAudioIndex, (selected) => {
            switchAudioTrack(selected);
        });
    }

    video.addEventListener("loadedmetadata", () => {
        refreshSubtitleTracks();
        refreshAudioTracks();
    });

    if (video.readyState >= 1) {
        refreshSubtitleTracks();
        refreshAudioTracks();
    }

    // Subtitles

    let subtitleOverlay = null;
    let currentSubtitleTrack = null;

    function ensureSubtitleOverlay() {
        if (!subtitleOverlay) {
            subtitleOverlay = document.createElement("div");
            subtitleOverlay.className = "subtitle-overlay";
            subtitleOverlay.hidden = true;
            stage.appendChild(subtitleOverlay);
        }
        return subtitleOverlay;
    }

    // NOTE ON SUBTITLE SYNC: the .vtt files served by /subtitles/{slug}/{index}.vtt
    // always contain cues timestamped against the ORIGINAL full-length video (t=0
    // at the very start of the episode/movie), and are cached that way forever.
    // But in transcoded/remux mode, seeking reloads the <video> from a server-side
    // offset, so video.currentTime resets to ~0 while seekBaseTime holds how far
    // into the real timeline that reload represents (see the big comment near the
    // top of this file). The browser's own cue-activation logic (TextTrack.activeCues,
    // and the "cuechange" event) is computed against the raw video.currentTime, so
    // trusting it after a transcoded/remux seek shows subtitles from the wrong moment
    // (typically ~seekBaseTime seconds off). Fix: don't rely on activeCues at all,
    // manually scan track.cues against the same corrected "displayTime" the scrubber
    // and clock already use (seekBaseTime + video.currentTime).
    function getCorrectedTime() {
        return (isTranscoded || isRemuxMode) ? seekBaseTime + video.currentTime : video.currentTime;
    }

    function renderActiveCues() {
        const overlay = ensureSubtitleOverlay();
        if (!currentSubtitleTrack || !currentSubtitleTrack.cues) {
            overlay.replaceChildren();
            overlay.hidden = true;
            return;
        }
        const now = getCorrectedTime();
        const cues = Array.from(currentSubtitleTrack.cues).filter(
            (cue) => cue.startTime <= now && now <= cue.endTime
        );
        if (cues.length === 0) {
            overlay.replaceChildren();
            overlay.hidden = true;
            return;
        }
        overlay.hidden = false;
        overlay.replaceChildren();
        cues.forEach((cue, i) => {
            if (i > 0) overlay.appendChild(document.createElement("br"));
            overlay.appendChild(
                typeof cue.getCueAsHTML === "function" ? cue.getCueAsHTML() : document.createTextNode(cue.text)
            );
        });
    }

    function setSubtitleTrack(track) {
        if (currentSubtitleTrack) {
            currentSubtitleTrack.mode = "disabled";
        }
        currentSubtitleTrack = track;
        if (currentSubtitleTrack) {
            // mode = "hidden" still loads/parses cues (track.cues populates) without
            // the browser painting them; we only use it as a cue data source now.
            // We no longer listen for "cuechange" since that event (and activeCues)
            // is keyed to the browser's raw, sometimes-wrong video.currentTime; the
            // timeupdate listener below drives rendering off the corrected time instead.
            currentSubtitleTrack.mode = "hidden";
        }
        renderActiveCues();
    }

    // Re-check active cues on every timeupdate (same tick the scrubber/clock use),
    // rather than only on "cuechange", so a transcoded/remux seek that resets
    // video.currentTime doesn't leave stale or wrongly-timed subtitles on screen.
    video.addEventListener("timeupdate", renderActiveCues);

    // Every video.load() (transcoded seek, audio track switch) discards and
    // re-creates the <track> elements' TextTrack objects, so currentSubtitleTrack
    // ends up pointing at a stale/detached object that's no longer in
    // video.textTracks. Re-arm it by matching on srclang/label against the new
    // list, so a reload doesn't silently reset the user's subtitle choice back
    // to "Off". Falls back to null (Off) if nothing matches (e.g. this episode
    // doesn't have that language).
    function reattachSubtitleTrackAfterReload() {
        if (!currentSubtitleTrack) return;
        const wantedLang = currentSubtitleTrack.language;
        const wantedLabel = currentSubtitleTrack.label;
        const tracks = Array.from(video.textTracks || []);
        const match = tracks.find((t) => t.language === wantedLang && t.label === wantedLabel);
        setSubtitleTrack(match || null);
    }

    function refreshSubtitleTracks() {
        const container = document.getElementById("subtitle-track-list");
        const tracks = Array.from(video.textTracks || []);
        const labels = ["Off", ...tracks.map((t, i) => t.label || `Track ${i + 1}`)];
        const activeIndex = tracks.findIndex((t) => t === currentSubtitleTrack);
        renderTrackList(container, labels, activeIndex + 1, (selected) => {
            setSubtitleTrack(selected === 0 ? null : tracks[selected - 1]);
            refreshSubtitleTracks();
        });
    }

    video.addEventListener("loadedmetadata", () => {
        reattachSubtitleTrackAfterReload();
        refreshSubtitleTracks();
    });

    // Scrubber thumbnail preview
    const scrubberMeta = (() => {
        try { return JSON.parse(video.dataset.scrubberMeta || 'null'); }
        catch { return null; }
    })();

    let thumbEl = null;

    function ensureThumb() {
        if (thumbEl) return thumbEl;
        thumbEl = document.createElement('div');
        thumbEl.className = 'scrubber-thumb';
        thumbEl.innerHTML = '<canvas class="scrubber-thumb-canvas"></canvas><span class="scrubber-thumb-time"></span>';
        controlsBar.appendChild(thumbEl);
        return thumbEl;
    }

    function showScrubberThumb(hoverSeconds, cursorXInScrubber) {
        if (!scrubberMeta) return;
        const { timestamps, thumb_w, thumb_h, cols, canvas_w } = scrubberMeta;

        // Find the closest frame to hoverSeconds using the real timestamps array
        let bestIdx = 0;
        let bestDiff = Infinity;
        timestamps.forEach((t, i) => {
            const diff = Math.abs(t - hoverSeconds);
            if (diff < bestDiff) { bestDiff = diff; bestIdx = i; }
        });

        const col = bestIdx % cols;
        const row = Math.floor(bestIdx / cols);
        const srcX = col * thumb_w;
        const srcY = row * thumb_h;

        const thumb = ensureThumb();
        const canvas = thumb.querySelector('canvas');
        const timeEl = thumb.querySelector('.scrubber-thumb-time');

        canvas.width = thumb_w;
        canvas.height = thumb_h;

        // Lazy-load the sprite sheet image once and reuse it
        if (!showScrubberThumb._img) {
            const img = new Image();
            if (!video.dataset.season || !video.dataset.episode) { // Assume movie with no season/episode query params
                img.src = `/scrubber/${video.dataset.slug}`;
            } else { // Assume TV episode with season/episode query params
                img.src = `/scrubber/${video.dataset.slug}?season=${video.dataset.season}&episode=${video.dataset.episode}`;
            }
            showScrubberThumb._img = img;
        }
        const img = showScrubberThumb._img;

        const draw = () => {
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, srcX, srcY, thumb_w, thumb_h, 0, 0, thumb_w, thumb_h);
        };
        if (img.complete) draw();
        else img.onload = draw;

        timeEl.textContent = formatTime(hoverSeconds);

        // Position: centred on cursor, clamped to scrubber row width
        const scrubberRect = scrubber.getBoundingClientRect();
        const thumbHalfW = thumb_w / 2;
        const clampedX = Math.max(thumbHalfW, Math.min(cursorXInScrubber, scrubberRect.width - thumbHalfW));
        thumb.style.left = `${clampedX}px`;
        thumb.style.transform = 'translateX(-50%)';
        thumb.hidden = false;
    }

    function hideScrubberThumb() {
        if (thumbEl) thumbEl.hidden = true;
    }

    let lastHoverSeconds = null;

    if (scrubberMeta) {
        scrubber.addEventListener('mousemove', (e) => {
            const rect = scrubber.getBoundingClientRect();
            const THUMB_RADIUS = 7;
            const trackWidth = rect.width - THUMB_RADIUS * 2;
            const fraction = Math.max(0, Math.min(1, (e.clientX - rect.left - THUMB_RADIUS) / trackWidth));
            lastHoverSeconds = fraction * (Number(scrubber.max) || 0);
            const barRect = controlsBar.getBoundingClientRect();
            const cursorXInBar = e.clientX - barRect.left;
            showScrubberThumb(lastHoverSeconds, cursorXInBar);
            clearTimeout(hideTimer);
        });
        scrubber.addEventListener('mouseleave', () => {
            lastHoverSeconds = null;
            hideScrubberThumb();
        });
    }

    scrubber.addEventListener("change", () => {
        const target = lastHoverSeconds !== null ? lastHoverSeconds : Number(scrubber.value);
        if (isTranscoded || isRemuxMode) {
            seekToTranscodedTime(target);
        } else {
            video.currentTime = target;
        }
        isScrubbing = false;
    });

    // Auto-hide (top overlay + bottom control bar + mouse cursor move together)
    function showControls() {
        overlay.classList.remove("hidden");
        controlsBar.classList.remove("hidden");
        stage.classList.remove("cursor-hidden");
        scheduleHide();
    }

    function scheduleHide() {
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => {
            if (!video.paused && !isScrubbing && !isSettingsOpen) {
                overlay.classList.add("hidden");
                controlsBar.classList.add("hidden");
                stage.classList.add("cursor-hidden");
            }
        }, HIDE_DELAY_MS);
    }

    if (!isMobile) {
        stage.addEventListener("mousemove", showControls);
        stage.addEventListener("mouseleave", () => {
            clearTimeout(hideTimer);
            if (!video.paused) {
                overlay.classList.add("hidden");
                controlsBar.classList.add("hidden");
                stage.classList.add("cursor-hidden");
            }
        });
    }

    video.addEventListener("pause", showControls);
    video.addEventListener("play", scheduleHide);

    // If ?timestamp= was passed in the URL, seek to that point on first load (in seconds)
    const urlParams = new URLSearchParams(window.location.search);
    const timestampParam = urlParams.get("timestamp");
    if (timestampParam) {
        const ts = parseFloat(timestampParam);
        if (!isNaN(ts) && ts >= 0) {
            if (isTranscoded || isRemuxMode) {
                seekToTranscodedTime(ts);
            } else {
                video.currentTime = ts;
            }
        }
    }

    // Initial state
    setPlayPauseIcon(!video.paused);
    setVolumeIcon();
    setFullscreenIcon();
    showControls();
})();