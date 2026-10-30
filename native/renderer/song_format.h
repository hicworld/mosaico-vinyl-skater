#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* All integers are little endian. This is the exact Python struct contract:
 * header <4sHH10I80s, beat <IHH, plan <IIHBBHH. The two target runtimes,
 * ESP32-S31 and the WASI validation executable, are both little endian. */
#define SONG_FORMAT_VERSION 3u
#define SONG_FORMAT_LEGACY_VERSION 2u
#define SONG_HEADER_BYTES 128u
#define SONG_SAMPLE_RATE 16000u
#define SONG_MAX_SECONDS (30u * 60u)
#define SONG_MAX_SAMPLES (SONG_SAMPLE_RATE * SONG_MAX_SECONDS)
#define SONG_MAX_BEATS 10000u
#define SONG_MAX_PLANS 5000u
#define SONG_CLIP_COUNT 55u
#define SONG_THEME_COUNT 7u
#define SONG_LEGACY_THEME_COUNT 4u
#define SONG_ACTIVE_THEME_COUNT 6u
#define SONG_RECORD_COLOR_COUNT 4u
#define SONG_EFFECT_COUNT 7u
#define SONG_THEME_TAG 0x54480000u
#define SONG_STYLE_TAG 0x56430000u
#define SONG_EFFECT_TAG 0x46580000u
#define SONG_LINEAR_FIRST_BEAT UINT16_MAX
#define SONG_BEAT_DOWNBEAT 1u
#define SONG_BEAT_SENTINEL 2u
#define SONG_BEAT_UNCERTAIN 4u
#define SONG_AUDIO_HEADER_BYTES 24u
#define SONG_ENVELOPE_HOP 320u
#define SONG_MAX_ENVELOPE_POINTS ((SONG_MAX_SAMPLES+SONG_ENVELOPE_HOP-1u)/SONG_ENVELOPE_HOP+1u)
#define SONG_MAX_DRUMS 18000u
#define SONG_MAX_AUDIO_BYTES (SONG_AUDIO_HEADER_BYTES+((SONG_MAX_ENVELOPE_POINTS+3u)&~3u)+SONG_MAX_DRUMS*8u)

typedef struct __attribute__((packed)) {
    char magic[4];
    uint16_t version, header_bytes;
    uint32_t sample_rate, sample_count, beat_count, plan_count;
    uint32_t beats_offset, plans_offset, pcm_offset, file_bytes;
    uint32_t payload_crc32, reserved;
    char title[80];
} song_header_t;

typedef struct __attribute__((packed)) {
    uint32_t sample;
    uint16_t strength, flags;
} song_beat_t;

typedef struct __attribute__((packed)) {
    uint32_t start_sample, end_sample;
    uint16_t first_beat;
    uint8_t beat_count, clip;
    uint16_t energy, reserved;
} song_plan_t;

typedef struct __attribute__((packed)) {
    char magic[4];
    uint16_t version,header_bytes;
    uint32_t envelope_hop,envelope_count,drum_count,reserved;
} song_audio_header_t;
typedef struct __attribute__((packed)) {
    uint32_t sample;
    uint16_t strength,kind;
} song_drum_t;
typedef struct {
    uint32_t envelope_hop,envelope_count,drum_count;
    const uint8_t *envelope;
    const song_drum_t *drums;
} song_audio_features_t;

_Static_assert(sizeof(song_header_t) == SONG_HEADER_BYTES, "song header size");
_Static_assert(offsetof(song_header_t, reserved) == 44, "song theme tag offset");
_Static_assert(sizeof(song_beat_t) == 8, "song beat size");
_Static_assert(sizeof(song_plan_t) == 16, "song plan size");
_Static_assert(sizeof(song_audio_header_t) == SONG_AUDIO_HEADER_BYTES,"audio header size");
_Static_assert(sizeof(song_drum_t) == 8,"drum event size");

bool song_format_header(const void *bytes, size_t bytes_available,
                        uint64_t actual_file_bytes, song_header_t *out);
/* Legacy zero / TH themes remain readable. Retired theme 0 aliases sunset 1.
 * New VC words encode independent record colour in bits 8..15 and canonical
 * legacy scene theme 1..3 in bits 0..7. Every other bit is part of the exact tag.
 * FX words add a 4-bit effect ID in bits 12..15 and a 4-bit record ID in
 * bits 8..11; FX themes 1..6 include the new compact nature backdrops.
 * Invalid values return the corresponding count sentinel. */
unsigned song_format_theme(const song_header_t *header);
unsigned song_format_record(const song_header_t *header);
unsigned song_format_effect(const song_header_t *header);
bool song_format_metadata(const song_header_t *header,
                          const song_beat_t *beats,
                          const song_plan_t *plans);
/* V3 has a complete VSA1 auxiliary track before PCM; V2 has no auxiliary
 * bytes and returns an empty view. Views borrow caller-owned storage. */
bool song_format_audio(const song_header_t *header,const void *bytes,
                       size_t bytes_available,song_audio_features_t *out);
/* CRC-32/ISO-HDLC: start with 0, then pass each returned value to the next
 * update. This matches Python zlib.crc32, including empty input. */
uint32_t song_crc32(uint32_t previous, const void *bytes, size_t length);
unsigned song_plan_find(const song_plan_t *plans, unsigned count,
                        uint32_t sample);
