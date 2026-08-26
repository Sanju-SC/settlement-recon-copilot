shop_amounts = [1000, 500, 2000]
bank_amounts = [980, 500, 1950]

for i in range(len(shop_amounts)):
    shop = shop_amounts[i]
    bank = bank_amounts[i]
    if shop == bank:
        print(f"Row {i}: shop says {shop}, bank says {bank} -> Match!")
    else:
        print(f"Row {i}: shop says {shop}, bank says {bank} -> These don't match.")