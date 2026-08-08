-- Dune query parameters (Number): StartBlock and EndBlock
-- The caller guarantees an inclusive range of at most 1000 blocks.
SELECT DISTINCT
    tx_hash,
    block_number
FROM dex.atomic_arbitrages
WHERE blockchain = 'ethereum'
  AND block_number >= {{StartBlock}}
  AND block_number <= {{EndBlock}}
ORDER BY block_number DESC, tx_hash DESC;
