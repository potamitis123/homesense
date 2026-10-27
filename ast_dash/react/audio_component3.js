import React from 'react';

export default function VideoPlayer(props) {
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
        sound3
    } = props;

    const handlePlay = () => {
        setProps({ volume: 0 });
    };

    const handleEndOrPause = () => {
        setProps({ volume: 1 });
    };

    const containerStyle = {
        width: width || '100%',
        display: 'flex',
        flexDirection: 'column'
    };

    const mediaStyle = {
        marginTop: '5px',
        width: '100%',
        height: height || 'auto'
    };

    const style_sound_labels = {
        margin: '5px',
        padding: '5px',
        display: 'flex',
        justifyContent: 'space-between',
        width: '100%'
    };

    const style_title = {
        margin: '5px',
        padding: '5px',
        textAlign: 'center',
        fontWeight: 'bold',
        fontSize: '15px',  // Διόρθωση από fontsize σε fontSize
    }

    return React.createElement('div', {
        className: 'flex flex-col max-w-1xl',
        style: containerStyle
    }, [
        // Title container
        React.createElement('div', {
            key: 'title-container',
            style: { width: '100%' }
        }, [
            React.createElement('label', {
                key: 'title-label',
                style: style_title,
            }, title),
        ]),

        // Video container
        React.createElement('div', {
            key: 'video-container',
            style: { width: '100%' }
        }, [
            React.createElement('video', {
                key: 'video-element',
                style: mediaStyle,
                onPlay: handlePlay,
                onEnded: handleEndOrPause,
                onPause: handleEndOrPause,
                controls: true,
                poster: poster,
                preload: "none",
                title: title,
                src: src,
                type: "audio/mpeg"
            }),
        ]),

        // Sound labels container
        React.createElement('div', {
            key: 'sound-labels',
            style: style_sound_labels,
        }, [
            React.createElement('label', {
                key: 'sound1-label',
                className: 'text-lg font-medium text-gray-700'
            }, sound1),
            React.createElement('label', {
                key: 'sound2-label',
                className: 'text-lg font-medium text-gray-700'
            }, sound2),
            React.createElement('label', {
                key: 'sound3-label',
                className: 'text-lg font-medium text-gray-700'
            }, sound3)
        ])
    ]);
}

VideoPlayer.defaultProps = {
    poster: "",
    title: "Video Player",
    src: "",
    volume: 1,
    width: '100%',
    height: 'auto',
    sound1: "",
    sound2: "",
    sound3: ""
};