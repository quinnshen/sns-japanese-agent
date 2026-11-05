# SNS Japanese Agent

**Learn Japanese from the social media videos you love.**

We believe language learning is more motivating—and easier to keep up—when it connects with your interests. Sports, gaming, travel, food: the videos you already enjoy can become material for understanding Japanese and building your vocabulary.

SNS Japanese Agent turns Japanese dialogue into sentence-by-sentence translations and grammar breakdowns. Understand what you hear, explore how people actually speak, and save useful sentences to revisit later.

## Core features

| Feature | How it helps you learn |
|---|---|
| **Sentence-by-sentence translation** | Follow Japanese dialogue alongside natural Chinese translations, replay individual lines, and read kanji with kana readings in parentheses. |
| **Grammar and expression breakdowns** | Expand a sentence to understand how its key parts work together: verb forms, clauses, casual speech, and tone. Explanations are in Chinese. |
| **Save and review** | Save sentences with their full explanations. Revisit a specific clip from your history or review your saved sentences. |

Transcripts come from speech recognition. Suspected errors and meanings that depend on visual context are flagged rather than silently rewritten as fact.

## How to learn

1. **Choose something you enjoy.** Open a video and the extension, then select the whole video or a clip.
2. **Prepare your study material.** Play the selected section normally. Transcripts, translations, and explanations are generated and saved as processing progresses.
3. **Study sentence by sentence.** Replay the clip with Japanese–Chinese subtitles. Open an explanation whenever a line interests or puzzles you.
4. **Save and revisit.** Keep the sentences you want to learn, then return through your recent clips or saved sentences.

The first pass requires playing the selected clip. Once material is saved, reviewing it or opening an explanation does not make another model call.

## Get started

The application runs as a **Chrome extension with a local backend**. The full learning workflow requires your own transcription and language model credentials.

[Installation and usage guide](app/README.md)

Video page support is being expanded. See the guide for currently supported pages and playback requirements; broader Chrome video support is on the roadmap.

## Roadmap

- **Video understanding:** use on-screen text, actions, and scenes to clarify details left out of the audio, including references such as “this” and “here.”
- **Curated grammar learning:** select useful patterns and expressions from dialogue, explaining their structure, use, and tone in context, with less repetition.
- **More SNS video pages:** support more video pages in Chrome so learners can follow their own interests.
- **Long-video reliability:** improve handling of ads, buffering, task recovery, and progress reporting, then validate longer study sessions.

These are planned improvements. The usage guide describes what is available today.

## Data and costs

- Recordings, transcripts, explanations, and saved sentences are stored locally. When cloud processing is enabled, the required audio and text are sent to the configured providers.
- Regular clips have a default **7-day** retention period. Saved sentences and their explanations are stored separately until you remove them.
- You supply your own Google Cloud project and OpenAI API key. Charges go to the accounts you configure; this repository includes no author credentials or cloud credits.
- Private configuration and learning records are excluded from Git. See [development and security guidance](DEVELOPMENT.md).

## Project layout

```text
app/
  extension/                  Chrome study panel, bilingual subtitles, and review
  backend/                    Local transcription, translation, explanations, and storage
  README.md                   Installation and usage guide
  TRANSCRIPTION.md            Transcription, time mapping, and recovery
  LEARNING.md                 Explanation rules, precomputation, and retries
scripts/                      Offline tests and Git security checks
docs/                         Historical integration test documentation
shorts-audio-capture-test/     Retained five-second audio capture test extension
shorts-audio-capture-bridge/   Local backend for that test
CHANGELOG.md                  Development version history
DEVELOPMENT.md                Development and security conventions
```

The application is developed under `app/`. The original five-second test remains available as a separate [integration check](docs/FIVE_SECOND_TEST.md).

## Version history

Current development version: **0.6.0**, with separate study and review paths, saved sentences, and automatic cleanup of regular clips.

See [CHANGELOG](CHANGELOG.md) for the development history.
