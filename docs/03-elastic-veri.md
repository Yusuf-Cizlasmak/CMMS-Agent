# 3. Elastic'ten CMMS verisini doğru çekmek

## 3.1 Önce veriyi tanıyın (Kibana Dev Tools)

```http
GET _cat/indices/cmms*?v&s=index            # hangi index'ler var, kaç belge
GET cmms-workorders/_mapping                 # alan adları ve TİPLERİ
GET cmms-workorders/_search?size=3           # örnek belgeler
GET cmms-workorders/_field_caps?fields=*     # alanlar aggregatable mı?
```

Cevaplamanız gereken sorular:

| Soru | Neden önemli | Nerede ayarlanır |
|------|--------------|------------------|
| Ekipman ID alanı hangisi, tipi `keyword` mı? | `terms` aggregation `text` alanda çalışmaz | `CMMS_FIELD_ASSET` |
| Arıza bildirim zamanı hangi alan? `date` mi? | Tüm zaman filtreleri ve MTBF buna dayanır | `CMMS_FIELD_CREATED_AT` |
| İş emri tipi nasıl kodlanmış? ("CM", "Arıza", "corrective"?) | Düzeltici/önleyici ayrımı her analizin temeli | `CMMS_CORRECTIVE_VALUES` |
| Durum değerleri? | Açık iş / PM uyumu hesapları | `CMMS_OPEN_STATUS_VALUES`, `CMMS_COMPLETED_STATUS_VALUES` |
| Duruş süresi alanı var mı? Yoksa başlangıç/bitiş? | MTTR | `CMMS_FIELD_DOWNTIME` (yoksa started/completed farkı kullanılır) |

> Alan `text` ise ve altında `.keyword` alt alanı varsa, `.env`'de
> `CMMS_FIELD_ASSET=asset_id.keyword` yazmanız yeterli.

`python -m cmms_agent doctor` bu kontrollerin bir kısmını otomatik yapar.

### Otomatik keşif: `discover`

Bu soruların çoğunu sizin yerinize cevaplayan bir komut var:

```bash
python -m cmms_agent discover                         # .env'deki index'ler
python -m cmms_agent discover --index fabrika-bakim-isemirleri --write .env.discovered
```

Komut şunları yapar:
1. `_field_caps` ile tüm alanları ve tiplerini alır (`.keyword` alt alanları
   ve `Lokasyon.Hat` gibi iç içe yollar dahil).
2. Türkçe ve İngilizce yaygın isimlere bakarak (`Ekipman`, `IsEmriTipi`,
   `AcilisTarihi`, `asset_id`, `created_at`...) her rol için uygun tipte en
   iyi alanı seçer.
3. Tip ve durum alanlarının **gerçek değerlerini** `terms` aggregation ile
   çeker, sonra `Arıza`/`CM` → düzeltici, `Periyodik Bakım`/`PM` → önleyici,
   `Açık`/`Beklemede` → açık iş gibi sınıflandırır.
4. Bunları güven seviyesi ve alternatiflerle `.env` formatında yazar.

Örnek çıktı (mapping verilmeden yüklenmiş Türkçe bir index):
```
CMMS_FIELD_ASSET=Ekipman.keyword   # keyword, güven: yüksek  (alternatifler: EkipmanAdi.keyword)
CMMS_FIELD_TYPE=IsEmriTipi.keyword   # keyword, güven: yüksek
CMMS_FIELD_CREATED_AT=AcilisTarihi   # date, güven: yüksek  (alternatifler: KapanisTarihi)
# CMMS_FIELD_STARTED_AT=   # BULUNAMADI — elle doldurun
CMMS_CORRECTIVE_VALUES=Arıza
CMMS_OPEN_STATUS_VALUES=Açık,Beklemede
#   wo_type alanının değerleri: Arıza (115), Kalibrasyon (74), Periyodik Bakım (59), Kestirimci (52)
```
Çıktıyı **mutlaka gözden geçirin.** Örneğin "Kestirimci" iş emirlerini
arıza sayıp saymayacağınız bir iş kararıdır; komut bunu sizin yerinize
vermez, sadece değerleri listeler. Mevcut `.env` dosyanıza asla dokunmaz.

> **Keşif neden kurulumda bir kez yapılıyor da her soruda yapılmıyor?**
> Şemayı her soruda LLM'e göstermek, her soruya bir LLM turu ve yüzlerce
> token ekler. Küçük model de doğru alanı yine tahmin etmek zorunda kalır.
> Kararı bir kez, insan onayıyla vermek hem hızlı hem güvenilirdir
> (bkz. [07-eski-agent-dersleri.md](07-eski-agent-dersleri.md)).

### Dikkat: `.keyword` alanları `_source` içinde yoktur

Dinamik mapping her string'i `text` olarak tanımlar ve altına bir `.keyword`
alt alanı (multi-field) ekler:
- **Filtre ve aggregation** için `Ekipman.keyword` kullanılır.
- **Belgeyi okurken** (`_source`) alan hâlâ `Ekipman` adını taşır.
  `Ekipman.keyword` diye bir anahtar yoktur.

Bu proje bunu `es_client.source_path()` / `project()` ile otomatik çözer:
`.env`'ye `Ekipman.keyword` yazmanız yeterli. İç içe nesneler
(`{"Lokasyon": {"Hat": "Hat-A"}}` → `Lokasyon.Hat`) de aynı şekilde okunur.
Bu hata ancak gerçekçi, Türkçe adlı bir index'le test edilince ortaya çıktı;
artık `tests/test_es_integration.py` bunu koruyor.

## 3.2 Güvenlik: sadece okuma yetkili API key

Agent'a asla `elastic` süper kullanıcısını vermeyin.

```http
POST _security/role/cmms_agent_reader
{
  "indices": [{
    "names": ["cmms-*"],
    "privileges": ["read", "view_index_metadata"]
  }]
}

POST _security/api_key
{
  "name": "cmms-agent-jetson",
  "role_descriptors": {
    "cmms_agent_reader": {
      "indices": [{ "names": ["cmms-*"], "privileges": ["read", "view_index_metadata"] }]
    }
  },
  "expiration": "365d"
}
```
Dönen `encoded` değerini `.env` içinde `ES_API_KEY=` olarak girin.
`read` yetkisi `_search`, PIT açma ve aggregation için yeterlidir.

TLS: Kurumsal/self-signed sertifika kullanılıyorsa CA dosyasını Jetson'a
kopyalayıp `ES_CA_CERTS` ile verin. `ES_VERIFY_CERTS=false` sadece geçici test
içindir.

## 3.3 Veri çekmenin dört yolu — hangisi ne zaman?

### (1) Aggregation (`size: 0`) — varsayılan tercih
Sayım, toplam, ortalama, gruplama, zaman serisi. Hesap Elastic
sunucusunda yapılır; ağdan sadece sonuç (birkaç KB) gelir.

```http
GET cmms-workorders/_search
{
  "size": 0,
  "query": { "bool": { "filter": [
    { "range": { "created_at": { "gte": "now-90d/d" } } },
    { "terms": { "type": ["corrective"] } }
  ]}},
  "aggs": {
    "assets": {
      "terms": { "field": "asset_id", "size": 5 },
      "aggs": { "downtime": { "sum": { "field": "downtime_hours" } } }
    }
  }
}
```
Bu projede: `kpi_summary`, `top_failing_assets`, `workorder_trend`,
`failure_modes`, `open_backlog`.

### (2) PIT + `search_after` — ham kayıt gerektiğinde
MTBF ve Weibull için her arızanın **zaman damgası** gerekir (aralıkları
hesaplamak için). Bu durumda sadece gereken alanları sayfa sayfa çekeriz:

```python
pit = es.open_point_in_time(index="cmms-workorders", keep_alive="1m")
resp = es.search(pit={"id": pit["id"], "keep_alive": "1m"},
                 query=..., source=["asset_id", "created_at", "downtime_hours"],
                 sort=[{"created_at": "asc"}, {"_shard_doc": "asc"}], size=2000)
# sonraki sayfa: search_after=resp["hits"]["hits"][-1]["sort"]
```
- `from/size` 10.000 belgeyi geçemez → kullanmayın.
- `scroll` eski yöntemdir; PIT önerilir.
- `_source` filtresi ağ trafiğini ve Jetson belleğini ciddi azaltır.
- `CMMS_MAX_DOCS` üst sınırı koruma içindir.

Bu projede: `es_client.py → iter_docs()`, kullanan araçlar
`asset_reliability`, `failure_risk_ranking`.

> **Optimizasyon fikri:** Çok büyük veri setlerinde, olaylar arası süreyi
> Elastic'te önceden hesaplayıp (ingest pipeline ya da transform ile)
> belgeye `hours_since_prev_failure` alanı olarak yazarsanız, MTBF de
> saf aggregation ile hesaplanabilir.

### (3) ES|QL (Elasticsearch 8.11+) — okunabilir analitik sorgular
```http
POST _query
{ "query": """
  FROM cmms-workorders
  | WHERE type == "corrective" AND created_at > NOW() - 90 days
  | STATS failures = COUNT(*), downtime = SUM(downtime_hours) BY asset_id
  | SORT failures DESC | LIMIT 5
""" }
```
Python: `es.esql.query(query=..., format="json")`. Yeni araç yazarken
denemek için harikadır; DSL'e göre çok daha okunaklıdır.

### (4) Tam metin arama — benzer arızaları bulmak
`description` alanı `turkish` analyzer'lı `text` olursa "rulmanlar",
"rulmandan" gibi çekimler de eşleşir. Ama dinamik mapping'de bu analyzer
**yoktur**: "rulman" araması "Rulmandan ses geliyor" kaydını bulamaz.
Bulanık eşleşme (`fuzziness`) de 3 harflik bir eki tolere etmez.

`search_workorders` bu yüzden iki sorguyu birleştirir (`should`):
- `multi_match` + `fuzziness: AUTO`: yazım hataları için ("rulamn").
- `simple_query_string` ile **önek** araması (`rulman*`): Türkçe eklemeli
  bir dil olduğu için, kök + `*` çekimlerin çoğunu yakalar.

Kalıcı çözüm: index mapping'inde açıklama alanına `"analyzer": "turkish"`
vermek (yeni bir index + reindex gerektirir).

İleride: açıklamaları bir embedding modeliyle vektöre çevirip `dense_vector`
alanına yazarak **anlamsal arama** ("motor ısınıyor" ≈ "aşırı sıcaklık
alarmı") yapabilirsiniz (bkz. doküman 5).

## 3.4 Performans ipuçları

- **Filter context** kullanın (`bool.filter`): skor hesaplanmaz ve sonuç
  önbelleğe alınır. Bu projedeki tüm filtreler böyledir.
- **Tarih yuvarlama:** `now-90d/d` (gün başına yuvarla) aynı gün içindeki
  tekrarlı sorguların Elastic *request cache*'ine denk gelmesini sağlar.
  `now-90d` her milisaniye değişir ve önbelleği işe yaramaz hale getirir.
- **Sadece ihtiyacınız olanı çekin:** `size: 0`, `_source` filtresi,
  `terms.size` küçük.
- Agent tarafında da `cache.py` (TTL 120 sn) aynı aracı + argümanı tekrar
  sorgulamaz.
- Jetson ile Elastic arasındaki ağ gecikmesi (RTT) her sorguya eklenir;
  aynı LAN'da olmaları idealdir.

## 3.5 Veri kalitesi — öngörünün kalitesini belirler

"Çöp girer, çöp çıkar." Güvenilirlik analizinden önce kontrol edin:

- **Zaman dilimi:** Tüm tarihler UTC ya da açık offset'li olmalı.
- **Tekrarlı kayıtlar:** Aynı arıza için birden çok iş emri açılıyorsa MTBF
  yapay olarak düşer. Gerekirse "ana iş emri" alanıyla filtreleyin.
- **Arıza tanımı:** Her düzeltici iş emri gerçek bir *fonksiyon kaybı*
  mıdır? "Ampul değişimi" ile "ana motor yandı" aynı sayılmamalı. Öncelik
  veya arıza kodu ile filtre eklemek iyi bir sonraki adımdır.
- **Eksik duruş süresi:** Kapanmamış işlerde `downtime_hours` boştur;
  MTTR hesabında bunlar otomatik dışlanır.

## 3.6 CMMS verisi henüz Elastic'te değilse

- **Logstash JDBC input** ile CMMS veritabanından (SQL Server, Oracle,
  PostgreSQL) periyodik, `updated_at` bazlı artımlı aktarım.
- Ya da basit bir Python ETL: `helpers.bulk()` ile (bkz.
  `scripts/seed_sample_data.py` — mapping örneği de orada).
- Belge `_id`'si olarak iş emri numarasını kullanın → tekrar aktarımda
  kopya oluşmaz, güncelleme olur.
