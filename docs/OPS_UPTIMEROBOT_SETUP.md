# Setup: monitor externo via UptimeRobot

> Objetivo: detectar quando o backend Blaxx Pontos cair ANTES dos usuários
> reportarem. UptimeRobot ping `/healthz` a cada 5min e dispara alerta por
> e-mail/SMS/webhook em qualquer falha consecutiva.

Plano gratuito: **50 monitores · ping cada 5min · alertas por e-mail
ilimitados**. Suficiente pra MVP + alguns ambientes.

---

## Passo 1 — Criar conta UptimeRobot

1. Acesse <https://uptimerobot.com/signUp>
2. Cadastre com seu e-mail (preferencialmente o mesmo de oncall)
3. Confirme via e-mail de ativação

## Passo 2 — Adicionar monitor de healthcheck

1. Dashboard → **Add New Monitor**
2. Preencha:

   | Campo                 | Valor                                              |
   |-----------------------|----------------------------------------------------|
   | Monitor Type          | **HTTP(s)**                                        |
   | Friendly Name         | `Blaxx Backend · /healthz`                         |
   | URL                   | `https://blaxx-pontos-backend.onrender.com/healthz`|
   | Monitoring Interval   | **5 minutes** (mínimo no plano free)               |
   | Monitor Timeout       | 30 seconds (Render free pode ter cold start ~10s)  |
   | HTTP Method           | GET                                                |
   | Expected Status Codes | `200`                                              |

3. **Alert Contacts**: marque seu e-mail (já vem cadastrado)

4. **Create Monitor**

Dentro de poucos minutos o monitor começa a pingar. Você vê histórico
visual de uptime + tempo de resposta.

## Passo 3 — Monitor adicional do site (opcional)

Para detectar Netlify caindo (raro mas possível):

| Campo                 | Valor                                              |
|-----------------------|----------------------------------------------------|
| Monitor Type          | HTTP(s)                                            |
| Friendly Name         | `Blaxx Site · Netlify`                             |
| URL                   | `https://blaxxpontos.netlify.app/`                 |
| Monitoring Interval   | 5 minutes                                          |
| Expected Status Codes | `200`                                              |
| Keyword Type          | **Should Exist**                                   |
| Keyword               | `Blaxx Pontos` (texto que aparece em qualquer página) |

A verificação por keyword pega casos onde Netlify retorna 200 mas conteúdo
está quebrado.

## Passo 4 — Configurar alertas extras (recomendado)

Por padrão UptimeRobot manda e-mail. Pra Telegram/WhatsApp/Slack:

1. **My Settings** → **Alert Contacts** → **Add Alert Contact**
2. Tipos disponíveis:
   - **Telegram**: cria bot via @BotFather, cola token
   - **Slack**: incoming webhook URL do canal
   - **Discord**: webhook do canal
   - **Webhook genérico**: POST para qualquer URL (ex: integração com PagerDuty)

3. Volta ao monitor → **Edit** → adiciona o novo Alert Contact

## Passo 5 — Public status page (opcional mas legal)

Pra mostrar uptime publicamente (lobby de cliente, equipe interna):

1. **Status Pages** → **Add New Status Page**
2. Configure:
   - **Custom Subdomain**: `status.blaxxpontos.com.br` (precisa de CNAME)
     OU `https://stats.uptimerobot.com/xxxx` (gratuito sem DNS)
   - Selecione quais monitores aparecem (Backend + Site)
   - Cores: customizable

3. **Save** — gera URL pública com uptime histórico

## Validação

Pra forçar um teste:

```bash
# 1. Verifique que o monitor responde
curl -s https://blaxx-pontos-backend.onrender.com/healthz
# {"service":"blaxx-pontos-backend","status":"ok","uptime_s":...}

# 2. UptimeRobot Dashboard mostra "Up" com tempo de resposta
# 3. Se quiser TESTAR alerta:
#    Mude temporariamente Expected Status Codes pra 999 (impossivel)
#    → próximo ping falha → você recebe e-mail em ~5min
#    Reverte pra 200 → alerta de "back up" chega
```

## Custos

Plano gratuito cobre:
- 50 monitores
- Interval mínimo 5min
- Alertas e-mail ilimitados
- Public status page

Se precisar:
- **1min interval**: Solo $7/mês
- **30s interval + SMS alerts**: Team $30/mês

Pra MVP / ambiente single backend, free é o suficiente.

## Limites do Render Free + UptimeRobot

O Render free tier hiberna serviços inativos após 15min. UptimeRobot
pingando a cada 5min **mantém o backend acordado** (efeito colateral
desejável — usuário nunca pega cold start).

Custo no plano free do Render:
- 750h CPU/mês total
- 12 pings/hora × 24h × 30 dias = 8.640 hits/mês × ~2ms = 17s CPU
- Negligenciável (~0.005% do budget)

## Alertas: o que esperar

| Cenário                       | Comportamento                                    |
|-------------------------------|--------------------------------------------------|
| Render cai (50x)              | E-mail "Down" em 5–10min                         |
| Cold start lento >30s         | E-mail "Down" depois "Up" em ~10min              |
| DB do Neon caiu               | `/healthz` continua 200 mas `/readyz` retornaria 503 (se adicionar monitor pra ele) |
| Domínio expirou               | E-mail "Down" — DNS Failure                      |

Pra detectar DB down sem alertar em cold-start lento:
- Adicione segundo monitor pra `/readyz` (que faz `SELECT 1` no DB)
- Threshold: 2 falhas consecutivas (10min) antes de alertar
