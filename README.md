# Preço da Hora Bahia — Agro & Varejo (Plataforma Web)

Plataforma Web completa desenvolvida a partir da engenharia reversa do aplicativo oficial **Preço da Hora Bahia (SEFAZ-BA)**, com foco especializado no setor agropecuário e distribuição de insumos (defensivos agrícolas, adubos e fertilizantes) da região do Vale do São Francisco (Juazeiro-BA / Petrolina-PE).

---

## 🚀 Funcionalidades

1. **Pesquisa Inteligente de Insumos & Produtos de Varejo:**
   - Autocomplete em tempo real com sugestões a partir de 2 letras.
   - Pesquisa por defensivos agrícolas (**Dormex**, **Paclo / Cultar**, **Belt**, **Delegate**, **Timorex**, **Bordalo**, adubos NPK, Ureia) e produtos de varejo em geral (alimentos, farmácia e higiene).
   - Suporte a busca direta por código de barras (**GTIN / EAN**).

2. **Auditoria Fiscal & Dados em Nota (NF-e Mod. 55 e NFC-e Mod. 65):**
   - Data e hora exata da emissão da última venda.
   - Modelo do documento fiscal (`NF-e Mod. 55` para atacado/agro ou `NFC-e Mod. 65` para varejo).
   - Número oficial de registro da Nota Fiscal.
   - Código de classificação fiscal (**NCM**) e grupo tributário.
   - Preço praticado em nota, valor bruto e descontos.

3. **Comparador entre Revendas Concorrentes:**
   - Exibição simultânea de todas as revendas que venderam o mesmo produto (ex: **Plantebem**, **Central de Adubos**, **Agropodas**).
   - Identificação automática do **Menor Preço** praticado na região.

4. **Histórico de Preços (Últimos 15 Dias):**
   - Gráfico interativo com a evolução diária dos preços praticados em nota fiscal por estabelecimento.

5. **Explorador por Loja / Revenda:**
   - Aba exclusiva para selecionar uma revenda específica e visualizar **todos os produtos e insumos vendidos por ela**.
   - Filtro instantâneo interno por nome ou NCM.

6. **Exportação de Cotações em CSV:**
   - Geração de planilha com layout completo (Produto, Fornecedor, CNPJ, Telefone, Preço, Unidade, Data de Emissão, Modelo e Número de NF-e, NCM e Distância).

---

## 🛠️ Tecnologias Utilizadas

- **Backend:** Python 3 (HTTP Server nativo, SQLite3 persistente, integração com API REST SEFAZ-BA).
- **Frontend:** HTML5, Tailwind CSS (CDN), Lucide Icons, Chart.js.
- **Autenticação SEFAZ:** Token JWT / Firebase Bearer Identity Toolkit extraído da engenharia reversa do binário ARM64 Dart (`libapp.so`).

---

## 📦 Como Executar

```bash
# Entrar no diretório do projeto
cd precodahora-web

# Iniciar o servidor
python server.py
```

Acesse no navegador: **`http://localhost:5050`**
