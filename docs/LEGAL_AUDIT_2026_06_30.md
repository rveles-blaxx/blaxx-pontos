# Auditoria Jurídica — BlaXx Pontos · 2026-06-30

> Gerada por agente Claude com base nos templates `PRIVACY_POLICY_TEMPLATE.md`,
> `TERMS_OF_SERVICE_TEMPLATE.md` e `BACEN_COMPLIANCE_CHECKLIST.md`. Este NÃO
> substitui revisão por advogado licenciado em direito digital/bancário.
> Serve para reduzir o custo da consulta jurídica formal entregando ao
> escritório um plano de ação concreto em vez de templates genéricos.

## A. LGPD (Lei 13.709/2018) — gaps

**Art. 6º — princípios incompletos.** Faltam menções explícitas a adequação,
necessidade, livre acesso, qualidade dos dados, prevenção, não discriminação
e accountability. Adicionar coluna "Princípio Art. 6º" na tabela de
finalidades da Seção 4 do Privacy Policy.

**Art. 8º §5º — consentimento versionado ausente.** Nenhum dos templates
descreve hash do termo aceito, timestamp, IP, evidência de revogação. Banner
de cookies precisa granularidade exigida pelo Guia ANPD 2023.

**Art. 41 — DPO não identificado.** `PRIVACY_POLICY_TEMPLATE.md` linhas 26-28
mantém `[NOME_DPO]` / `[TELEFONE_DPO]` como placeholders.
**P0: bloqueador formal de publicação.**

**Art. 48 — incidentes incompletos.** Seção 10 cita "2 dias úteis" alinhado à
Resolução CD/ANPD 15/2024, mas faltam: gatilho de severidade, modelo de
comunicado ao titular, escalonamento interno, referência a `INCIDENT_RESPONSE.md`
(citado no checklist mas inexistente).

**Art. 33 — transferência internacional frágil.** Substituir genérico "cláusulas
contratuais padrão" por menção expressa à **Resolução CD/ANPD 19/2024** (modelo
homologado em agosto/2024). Documentar PSP PIX (`[PSP_PIX]` em branco hoje).

## B. Pontos não são moeda (Lei 12.865/2013)

Seção 6 do ToS (linhas 72-96) bem construída — afasta moeda eletrônica,
depósito, investimento, criptoativo. **Mas contradição na Seção 7.2 + 8:**
admitir reembolso em dinheiro (Art. 49 CDC) E permitir Resgate PIX cria
lastro monetário *de facto*. BCB julga substância sobre forma (Carta-Circular
3.927/2019). **Disclaimer da Seção 6.4 NÃO blinda.**

Faltam:
- Distinção **Pontos comprados via PIX** vs **Pontos promocionais** (acúmulo)
- Aviso de que resgate PIX é **liberalidade comercial**, não direito patrimonial
- Disclaimer FGC (mencionado isoladamente — deve estar no fluxo de compra)

## C. Risco de Arranjo de Pagamento (BCB 80/2021)

Limites do checklist (linhas 22-23) corretos: R$ 500M/ano + R$ 50M saldo.

**Combinação P2P + compra PIX + resgate PIX = arranjo integrado** (Art. 6º
Lei 12.865/2013). Mesmo abaixo de R$ 500M, **obrigação de comunicação ao
BACEN** (Circular 3.682/2013 + Resolução BCB 150/2021).

BCB 80/2021 art. 6º classifica como arranjo regulado, independente de volume,
quando:
- (i) emite instrumento de uso restrito aceito por +1 recebedor (✓ os parceiros)
- (ii) movimenta recursos de terceiros (✓ P2P transfer)

Escala de risco:
| Volume anual | Ação |
|--------------|------|
| > R$ 10M | Consulta formal DECON/BACEN recomendada |
| > R$ 100M | Preparar pedido de autorização IP (análise 12-24 meses) |
| > R$ 500M | **Autorização obrigatória** |

## D. CDC (Lei 8.078/1990)

**Art. 49 (arrependimento) — cláusula abusiva.** Seção 7.2 condiciona à
"Pontos não utilizados, transferidos ou resgatados" — STJ REsp 1.340.604
anula esse tipo de restrição. CDC garante 7 dias incondicionalmente.

**Falha PIX — cláusula abusiva.** Seção 13 isenta de "falhas de terceiros
(PIX)". Art. 51 I + Art. 7º §único: responsabilidade solidária do fornecedor
prevalece. Reescrever para "envidaremos esforços para resolver, mediando".

**consumidor.gov.br** ausente (Lei 13.460/2017 recomenda).

**SAC 24/7** ausente (Decreto 11.034/2022 exige para serviços com transação
financeira).

## E. Red flags concretos — corrigir ANTES da consulta jurídica

1. **Privacy Policy linhas 26-28**: preencher DPO real ou interino.
2. **Privacy Policy Seção 4 (linhas 59-73)**: adicionar coluna "Princípio Art. 6º".
3. **Privacy Policy Seção 6 (linhas 94-101)**: citar Resolução CD/ANPD 19/2024 nominalmente.
4. **Privacy Policy Seção 12 (linhas 169-176)**: descrever critérios antifraude (Art. 20 — direito a explicação).
5. **ToS Seção 6.4 (linha 96)**: adicionar "esta operação opera atualmente sob hipótese de programa de fidelidade não regulado".
6. **ToS Seção 7.2 (linhas 107-112)**: REMOVER condicionante "desde que Pontos não tenham sido utilizados".
7. **ToS Seção 13 (linha 194)**: reescrever isenção PIX para "responsabilidade solidária".
8. **ToS Seção 9.2 (linhas 134-140)**: P2P irreversível — adicionar janela 60s de cancelamento.
9. **ToS Seção 6.3 (linha 90)**: definir validade dos Pontos (sem prazo = saldo de pagamento).
10. **BACEN checklist linha 71**: programa PLD/FT formalizado **antes** de operar (Circular BCB 3.978/2020 — independe de classificação como IP).

## Plano priorizado

| # | Prioridade | Ação | Custo estimado |
|---|-----------|------|----------------|
| 1 | **P0 (bloqueia launch)** | Designar DPO + preencher placeholders | R$ 0 |
| 2 | **P0** | Programa PLD/FT documentado + diretor responsável | R$ 15-40k |
| 3 | **P0** | Reescrever ToS Seção 7.2 e 13 (cláusulas abusivas) | R$ 3-8k |
| 4 | **P1 (antes de scale)** | Parecer formal BACEN (escritório especializado) | R$ 25-80k |
| 5 | **P1** | DPA com Render, Sentry, FCM, APNS, PSP PIX | R$ 5-15k |
| 6 | **P1** | RIPD para KYC e dados financeiros | R$ 8-20k |
| 7 | **P2 (antes de 1k usuários)** | KYC formal (Idwall/Caf/Unico) com listas OFAC/COAF | R$ 0.30-2/consulta |
| 8 | **P2** | Consentimento versionado (hash + IP + timestamp) | R$ 0 (dev) |
| 9 | **P2** | Criar `INCIDENT_RESPONSE.md` mencionado mas inexistente | R$ 0 |
| 10 | **P3 (antes de R$ 10M/ano)** | Consulta formal DECON/BACEN | R$ 10-25k |

## Risco residual sem essas correções

- Autuação ANPD: até 2% do faturamento, **máx R$ 50M**
- Autuação BCB por operação não autorizada (Art. 44 Lei 4.595/1964)
- Ações coletivas consumeristas (Defensorias, MP)

Documento mais frágil: **TERMS_OF_SERVICE_TEMPLATE.md**.
Privacy Policy em estado razoável.
BACEN checklist é honesto sobre gaps mas precisa virar plano de ação datado.
