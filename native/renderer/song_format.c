#include "song_format.h"
#include <string.h>

static bool valid_title(const char *s, size_t capacity)
{
    const unsigned char *p = (const unsigned char *)s;
    size_t n = 0;
    while (n < capacity && p[n]) ++n;
    if (!n || n == capacity) return false;
    /* Reject incomplete UTF-8, overlong encodings, surrogates, control bytes
     * and values beyond Unicode. Paths are never derived from this title. */
    for (size_t i = 0; i < n;) {
        uint32_t cp = p[i++], minimum = 0;
        unsigned more = 0;
        if (cp < 0x80) {
            if (cp < 0x20 || cp == 0x7f) return false;
            continue;
        }
        if (cp >= 0xc2 && cp <= 0xdf) { cp &= 31; more = 1; minimum = 0x80; }
        else if (cp >= 0xe0 && cp <= 0xef) { cp &= 15; more = 2; minimum = 0x800; }
        else if (cp >= 0xf0 && cp <= 0xf4) { cp &= 7; more = 3; minimum = 0x10000; }
        else return false;
        if (n - i < more) return false;
        for (unsigned j = 0; j < more; ++j) {
            unsigned c = p[i++];
            if ((c & 0xc0) != 0x80) return false;
            cp = (cp << 6) | (c & 63);
        }
        if (cp < minimum || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) return false;
    }
    return true;
}

unsigned song_format_theme(const song_header_t *h)
{
    if (!h) return SONG_THEME_COUNT;
    if (!h->reserved) return 1;
    unsigned theme=h->reserved & 0xffffu;
    if(theme<SONG_LEGACY_THEME_COUNT && h->reserved==(SONG_THEME_TAG|theme))return theme?theme:1;
    theme=h->reserved&255u;
    unsigned record=(h->reserved>>8)&255u;
    if(theme>0 && theme<SONG_LEGACY_THEME_COUNT && record<SONG_RECORD_COLOR_COUNT &&
        h->reserved==(SONG_STYLE_TAG|(record<<8)|theme))return theme;
    record=(h->reserved>>8)&15u;
    unsigned effect=(h->reserved>>12)&15u;
    return theme>0 && theme<SONG_THEME_COUNT && record<SONG_RECORD_COLOR_COUNT &&
        effect<SONG_EFFECT_COUNT && h->reserved==(SONG_EFFECT_TAG|(effect<<12)|(record<<8)|theme)
        ? theme : SONG_THEME_COUNT;
}

unsigned song_format_record(const song_header_t *h)
{
    if(song_format_theme(h)>=SONG_THEME_COUNT)return SONG_RECORD_COLOR_COUNT;
    if((h->reserved&0xffff0000u)==SONG_EFFECT_TAG)return (h->reserved>>8)&15u;
    return (h->reserved&0xffff0000u)==SONG_STYLE_TAG?(h->reserved>>8)&255u:0;
}

unsigned song_format_effect(const song_header_t *h)
{
    if(song_format_theme(h)>=SONG_THEME_COUNT)return SONG_EFFECT_COUNT;
    return (h->reserved&0xffff0000u)==SONG_EFFECT_TAG?(h->reserved>>12)&15u:0;
}

bool song_format_header(const void *bytes, size_t available,
                        uint64_t actual_file_bytes, song_header_t *out)
{
    if (!bytes || !out || available < SONG_HEADER_BYTES) return false;
    song_header_t h;
    memcpy(&h, bytes, sizeof(h));
    bool legacy=h.version==SONG_FORMAT_LEGACY_VERSION&&!memcmp(h.magic,"VSK2",4);
    bool reactive=h.version==SONG_FORMAT_VERSION&&!memcmp(h.magic,"VSK3",4);
    if ((!legacy&&!reactive) ||
        h.header_bytes != SONG_HEADER_BYTES || h.sample_rate != SONG_SAMPLE_RATE ||
        !h.sample_count || h.sample_count > SONG_MAX_SAMPLES ||
        !h.beat_count || h.beat_count > SONG_MAX_BEATS ||
        !h.plan_count || h.plan_count > SONG_MAX_PLANS || song_format_theme(&h)>=SONG_THEME_COUNT ||
        song_format_record(&h)>=SONG_RECORD_COLOR_COUNT ||
        song_format_effect(&h)>=SONG_EFFECT_COUNT ||
        h.file_bytes != actual_file_bytes || !valid_title(h.title, sizeof(h.title))) return false;
    uint64_t plan_offset = SONG_HEADER_BYTES + (uint64_t)h.beat_count * sizeof(song_beat_t);
    uint64_t metadata_end = plan_offset + (uint64_t)h.plan_count * sizeof(song_plan_t);
    if ((legacy&&h.pcm_offset!=metadata_end)||
        (reactive&&(h.pcm_offset<metadata_end+SONG_AUDIO_HEADER_BYTES||
                    h.pcm_offset>metadata_end+SONG_MAX_AUDIO_BYTES)))return false;
    uint64_t file_bytes = h.pcm_offset + (uint64_t)h.sample_count * sizeof(int16_t);
    if (h.beats_offset != SONG_HEADER_BYTES || h.plans_offset != plan_offset ||
        h.file_bytes != file_bytes) return false;
    *out = h;
    return true;
}

bool song_format_audio(const song_header_t *h,const void *bytes,size_t available,
                       song_audio_features_t *out)
{
    if(!h||!out)return false;
    memset(out,0,sizeof(*out));
    if(h->version==SONG_FORMAT_LEGACY_VERSION)return available==0;
    if(h->version!=SONG_FORMAT_VERSION||!bytes||available<SONG_AUDIO_HEADER_BYTES)return false;
    song_audio_header_t audio;memcpy(&audio,bytes,sizeof(audio));
    uint32_t count=(h->sample_count+SONG_ENVELOPE_HOP-1u)/SONG_ENVELOPE_HOP+1u;
    if(memcmp(audio.magic,"VSA1",4)||audio.version!=1||audio.header_bytes!=SONG_AUDIO_HEADER_BYTES||
       audio.envelope_hop!=SONG_ENVELOPE_HOP||audio.envelope_count!=count||
       count>SONG_MAX_ENVELOPE_POINTS||audio.drum_count>SONG_MAX_DRUMS||audio.reserved)return false;
    size_t padded=((size_t)count+3u)&~(size_t)3u;
    size_t expected=SONG_AUDIO_HEADER_BYTES+padded+(size_t)audio.drum_count*sizeof(song_drum_t);
    uint64_t metadata_end=(uint64_t)h->plans_offset+(uint64_t)h->plan_count*sizeof(song_plan_t);
    if(expected!=available||metadata_end+expected!=h->pcm_offset)return false;
    const uint8_t *envelope=(const uint8_t*)bytes+SONG_AUDIO_HEADER_BYTES;
    if(envelope[0])return false;
    for(size_t i=count;i<padded;i++)if(envelope[i])return false;
    const song_drum_t *drums=(const song_drum_t*)(envelope+padded);
    for(unsigned i=0;i<audio.drum_count;i++)
        if(drums[i].sample>=h->sample_count||!drums[i].strength||drums[i].kind!=1||
           (i&&drums[i].sample<=drums[i-1].sample))return false;
    *out=(song_audio_features_t){.envelope_hop=audio.envelope_hop,.envelope_count=count,
        .drum_count=audio.drum_count,.envelope=envelope,.drums=drums};
    return true;
}

bool song_format_metadata(const song_header_t *h, const song_beat_t *beats,
                          const song_plan_t *plans)
{
    if (!h || !beats || !plans || !h->beat_count || h->beat_count > SONG_MAX_BEATS ||
        !h->plan_count || h->plan_count > SONG_MAX_PLANS) return false;
    for (unsigned i = 0; i < h->beat_count; ++i) {
        if (beats[i].sample > h->sample_count || (beats[i].flags & ~7u) ||
            (i && beats[i].sample <= beats[i - 1].sample)) return false;
    }
    uint32_t end = 0;
    for (unsigned i = 0; i < h->plan_count; ++i) {
        const song_plan_t *p = plans + i;
        if (p->start_sample != end || p->end_sample <= end ||
            p->end_sample > h->sample_count || p->clip >= SONG_CLIP_COUNT || p->reserved) return false;
        if (p->first_beat == SONG_LINEAR_FIRST_BEAT) {
            if (p->beat_count || p->clip != 0) return false;
        } else {
            unsigned last = (unsigned)p->first_beat + p->beat_count;
            if (!p->beat_count || last >= h->beat_count ||
                beats[p->first_beat].sample != p->start_sample ||
                beats[last].sample != p->end_sample) return false;
        }
        end = p->end_sample;
    }
    return end == h->sample_count;
}

uint32_t song_crc32(uint32_t previous, const void *bytes, size_t length)
{
    uint32_t crc = ~previous;
    const uint8_t *p = bytes;
    for (size_t i = 0; i < length; ++i) {
        crc ^= p[i];
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

unsigned song_plan_find(const song_plan_t *plans, unsigned count, uint32_t sample)
{
    if (!plans || !count) return 0;
    unsigned lo = 0, hi = count;
    while (lo + 1 < hi) {
        unsigned middle = lo + (hi - lo) / 2;
        if (plans[middle].start_sample <= sample) lo = middle;
        else hi = middle;
    }
    return lo;
}
