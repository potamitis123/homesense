window.dash_clientside = Object.assign({}, window.dash_clientside, {
    clientside: {
        sync_audio_spectrogram: function(audio_src, graph_id, audio_id) {
            console.log("Syncing audio/spectrogram:", audio_src, graph_id, audio_id);
            if (!audio_src) return window.dash_clientside.no_update;

            const audio = document.getElementById(audio_id);
            const graph = document.getElementById(graph_id);

            if (!audio || !graph) {
                console.warn("Audio or Graph element not found:", !!audio, !!graph);
                return window.dash_clientside.no_update;
            }

            // Clear any existing interval/animation
            if (audio._syncFrame) {
                cancelAnimationFrame(audio._syncFrame);
            }

            const updateBar = () => {
                if (!audio.paused && !audio.ended) {
                    const duration = audio.duration;
                    const currentTime = audio.currentTime;
                    
                    if (duration && graph.layout) {
                        const position = (currentTime / duration) * 100;
                        
                        Plotly.relayout(graph, {
                            'shapes[0].x0': position,
                            'shapes[0].x1': position,
                        });
                    }
                }
                audio._syncFrame = requestAnimationFrame(updateBar);
            };

            audio._syncFrame = requestAnimationFrame(updateBar);
            
            return window.dash_clientside.no_update;
        },
        
        play_audio: function(n_clicks, audio_id) {
             const audio = document.getElementById(audio_id);
             if(audio){
                 audio.play().catch(e => console.log("Autoplay prevented or error", e));
             }
             return window.dash_clientside.no_update;
        },

        force_play: function(src, audio_id) {
            console.log("Force play requested for:", src);
            if (!src) return window.dash_clientside.no_update;
            
            const audio = document.getElementById(audio_id);
            if (audio) {
                // We let Dash handle setting the 'src' attribute.
                // We just ensure it plays.
                setTimeout(() => {
                    audio.play().catch(e => {
                        console.warn("Autoplay blocked or playback interrupted. User interaction may be required.", e);
                    });
                }, 100); // Tiny delay to let the 'src' stabilize
            }
            return window.dash_clientside.no_update;
        },

        setup_ptt_listeners: function(btn_id) {
            const btn = document.getElementById(btn_id);
            if (!btn || btn._pttAttached) return window.dash_clientside.no_update;

            console.log("Setting up PTT listeners for", btn_id);
            
            const handleStart = (e) => {
                if (e.button !== 0 && e.type.startsWith('mouse')) return;
                console.log("PTT Start");
                window.dash_clientside.set_ptt_state(true);
            };

            const handleEnd = (e) => {
                console.log("PTT End");
                window.dash_clientside.set_ptt_state(false);
            };

            btn.addEventListener('mousedown', handleStart);
            btn.addEventListener('mouseup', handleEnd);
            btn.addEventListener('mouseleave', handleEnd);
            
            // Touch support
            btn.addEventListener('touchstart', (e) => { e.preventDefault(); handleStart(e); });
            btn.addEventListener('touchend', handleEnd);

            btn._pttAttached = true;
            return window.dash_clientside.no_update;
        },

        set_ptt_state: function(active) {
            // This is a helper to update the store via a hidden button or similar
            // But in Dash, we can use a clientside callback that listens to the store.
            // Actually, we'll use this to trigger a Dash callback.
            const store = document.getElementById('ptt-active-store');
            if (store) {
                // We can't directly set store.data from here easily in a way Dash likes
                // unless we use a custom event or click a hidden button.
                const trigger = document.getElementById('ptt-trigger-btn');
                if (trigger) {
                    trigger.setAttribute('data-active', active);
                    trigger.click();
                }
            }
        }
    }
});
