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

    const style = {
        marginTop: '10px',
        width: width || '100%',
        height: height || 'auto'
    };

    const style_sound_labels = {
        margin: '10px',
        padding: '10px',
    };

    const style_title = {
        margin: '10px',
        padding: '10px',
        textAlign: 'center',
        fontWeight: 'bold',
        fontsize: '150px',
    }
    return React.createElement('div', {
        className: 'flex flex-col max-w-2xl'
    }, [
        React.createElement('div', {
            //className: 'flex flex-col max-w-2xl'
        }, [
            // Title label
            React.createElement('label', {
                key: 'title-label',
                style:style_title,
                //className: 'text-lg font-medium mb-2 text-gray-700'
            }, title),   
        ],),
        
        // Video element
        React.createElement('video', {
            key: 'video-element',
            style,
            onPlay: handlePlay,
            onEnded: handleEndOrPause,
            onPause: handleEndOrPause,
            controls: true,
            poster: poster,
            preload: "none",
            title: title,
            
        }, [
            React.createElement('source', {
                key: 'video-source',
                src: src,
                type: "audio/mpeg"
            })
        ]),
        React.createElement('div', {
            className: 'flex  max-w-2xl ',
            style:style_sound_labels,
        }, [
            React.createElement('label', {
                key: 'sound1-label',
                style:style_sound_labels,
                className: 'text-lg font-medium mb-2 text-gray-700 '
            },sound1),
            React.createElement('label', {
                key: 'sound2-label',
                style:style_sound_labels,
                className: 'text-lg font-medium mb-2 text-gray-700 '
            },sound2),
            React.createElement('label', {
                key: 'sound3-label',
                style:style_sound_labels,
                className: 'text-lg font-medium mb-2 text-gray-700 '
            },sound3),
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