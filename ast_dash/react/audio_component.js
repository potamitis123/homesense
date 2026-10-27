import React from 'react';

export default function AudioPlayer(props) {
    const {
        setProps,
        poster,
        title,
        src,
        volume,
        width,
        height,
        sound1,
        sound2,
        sound3,
        n_clicks,
    } = props;

    const handlePlay = () => {
        setProps({ volume: 0 });
    };

    const handleEndOrPause = () => {
        setProps({ volume: 1 });
    };

    const handleImageClick = () => {
        setProps({ n_clicks: (n_clicks || 0) + 1 });
    };

    const containerStyle = {
        width: width || '100%',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center'
    };

    const style_image = {
        marginTop: '10px',
        width: '100%',
        height: height - 30 || 'auto'
    };

    const style_audio = {
        width: '100%',
        marginTop: '10px'
    };

    const style_sound_labels = {
        margin: '5px',
        padding: '5px',
        width: '100%',
        display: 'flex',
        justifyContent: 'space-between',
        
    };

    const style_title = {
        margin: '5px',
        padding: '5px',
        textAlign: 'center',
        fontWeight: 'bold',
        fontsize: '150px',
    }

    return React.createElement('div', {
        className: 'flex flex-col max-w-2xl',
        style: containerStyle
    }, [
        React.createElement('div', {
            key: 'title-container',
            style: { width: '100%' }
        }, [
            // Title label
            React.createElement('label', {
                key: 'title-label',
                style: style_title,
            }, title),
        ]),

        // Image container
        React.createElement('div', {
            key: 'media-container',
            style: { width: '100%' }
        }, [
            // Image element
            React.createElement('img', {
                key: 'spectrogram-image',
                src: poster,
                alt: title,
                style: style_image,
                onClick: handleImageClick
            }),

            // Audio element
            React.createElement('audio', {
                key: 'audio-element',
                style: style_audio,
                onPlay: handlePlay,
                onEnded: handleEndOrPause,
                onPause: handleEndOrPause,
                controls: true,
                preload: "none",
                title: title,
                key: 'audio-source_mp3',
                src: src,
                type: "audio/mp3"
            }, )
        ]),

        // Sound labels
        React.createElement('div', {
            key: 'sound-labels',
            style: style_sound_labels,
        }, [
            React.createElement('label', {
                key: 'sound1-label',
                className: 'text-base font-medium mb-2 text-gray-700'
            }, sound1),
            React.createElement('label', {
                key: 'sound2-label',
                className: 'text-sm font-medium mb-2 text-gray-700'
            }, sound2),
            React.createElement('label', {
                key: 'sound3-label',
                className: 'text-xs font-medium mb-2 text-gray-700'
            }, sound3),
        ])
    ]);
}

AudioPlayer.defaultProps = {
    poster: "",
    title: "Audio Player",
    src: "",
    volume: 1,
    width: '100%',
    height: 'auto',
    sound1: "",
    sound2: "",
    sound3: "",
    n_clicks: 0
};