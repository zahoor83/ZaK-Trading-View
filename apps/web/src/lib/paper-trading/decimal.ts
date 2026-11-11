const MAX_DECIMAL_SCALE = 100;
const MAX_DECIMAL_TEXT_LENGTH = 128;

interface DecimalValue {
  coefficient: bigint;
  scale: number;
}

function assertScale(scale: number): void {
  if (
    !Number.isInteger(scale) ||
    scale < 0 ||
    scale > MAX_DECIMAL_SCALE
  ) {
    throw new Error("Decimal precision is outside the supported range.");
  }
}

function powerOfTen(exponent: number): bigint {
  if (
    !Number.isInteger(exponent) ||
    exponent < 0 ||
    exponent > MAX_DECIMAL_SCALE * 2
  ) {
    throw new Error("Decimal exponent is outside the supported range.");
  }

  return 10n ** BigInt(exponent);
}

function parseDecimal(value: number | string): DecimalValue {
  if (typeof value === "number" && !Number.isFinite(value)) {
    throw new Error("Decimal input must be finite.");
  }

  if (typeof value !== "number" && typeof value !== "string") {
    throw new Error("Decimal input must be a number or string.");
  }

  const text = typeof value === "string" ? value : value.toString();

  if (text.trim() !== text || text.length === 0) {
    throw new Error("Decimal input must not contain whitespace or be empty.");
  }

  if (text.length > MAX_DECIMAL_TEXT_LENGTH) {
    throw new Error("Decimal input exceeds the supported length.");
  }

  const match = /^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(text);

  if (!match) {
    throw new Error("Decimal input has an unsupported representation.");
  }

  const sign = match[1] === "-" ? -1n : 1n;
  const integerPart = match[2];
  const fractionalPart = match[3] ?? "";
  const exponent = Number(match[4] ?? "0");

  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > MAX_DECIMAL_SCALE) {
    throw new Error("Decimal exponent is outside the supported range.");
  }

  const digits = `${integerPart}${fractionalPart}`;

  let coefficient = BigInt(digits) * sign;
  let scale = fractionalPart.length - exponent;

  if (scale < 0) {
    coefficient *= powerOfTen(-scale);
    scale = 0;
  }

  if (scale > MAX_DECIMAL_SCALE) {
    throw new Error("Decimal scale exceeds the supported precision.");
  }

  while (scale > 0 && coefficient % 10n === 0n) {
    coefficient /= 10n;
    scale -= 1;
  }

  return { coefficient, scale };
}

function divideRounded(numerator: bigint, denominator: bigint): bigint {
  if (denominator <= 0n) {
    throw new Error("Decimal denominator must be positive.");
  }

  const negative = numerator < 0n;
  const absoluteNumerator = negative ? -numerator : numerator;

  const quotient = absoluteNumerator / denominator;
  const remainder = absoluteNumerator % denominator;

  const rounded =
    remainder * 2n >= denominator
      ? quotient + 1n
      : quotient;

  return negative ? -rounded : rounded;
}

function rescale(
  coefficient: bigint,
  currentScale: number,
  targetScale: number,
): bigint {
  if (
    !Number.isInteger(currentScale) ||
    currentScale < 0 ||
    currentScale > MAX_DECIMAL_SCALE * 2
  ) {
    throw new Error("Intermediate decimal scale is outside the supported range.");
  }

  assertScale(targetScale);

  if (currentScale === targetScale) {
    return coefficient;
  }

  if (currentScale < targetScale) {
    return coefficient * powerOfTen(targetScale - currentScale);
  }

  return divideRounded(
    coefficient,
    powerOfTen(currentScale - targetScale),
  );
}

function alignDecimals(
  left: DecimalValue,
  right: DecimalValue,
): [bigint, bigint, number] {
  const commonScale = Math.max(left.scale, right.scale);

  return [
    rescale(left.coefficient, left.scale, commonScale),
    rescale(right.coefficient, right.scale, commonScale),
    commonScale,
  ];
}

export function toScaledInteger(
  value: number | string,
  targetScale: number,
): bigint {
  assertScale(targetScale);

  const decimal = parseDecimal(value);

  return rescale(
    decimal.coefficient,
    decimal.scale,
    targetScale,
  );
}

export function multiplyToScaledInteger(
  left: number | string,
  right: number | string,
  targetScale: number,
): bigint {
  assertScale(targetScale);

  const leftDecimal = parseDecimal(left);
  const rightDecimal = parseDecimal(right);

  const coefficient =
    leftDecimal.coefficient * rightDecimal.coefficient;

  const combinedScale = leftDecimal.scale + rightDecimal.scale;

  if (combinedScale > MAX_DECIMAL_SCALE * 2) {
    throw new Error("Multiplication precision exceeds the supported range.");
  }

  return rescale(coefficient, combinedScale, targetScale);
}

export function differenceMultiplyToScaledInteger(
  left: number | string,
  right: number | string,
  multiplier: number | string,
  targetScale: number,
): bigint {
  assertScale(targetScale);

  const leftDecimal = parseDecimal(left);
  const rightDecimal = parseDecimal(right);
  const multiplierDecimal = parseDecimal(multiplier);

  const [alignedLeft, alignedRight, differenceScale] =
    alignDecimals(leftDecimal, rightDecimal);

  const difference = alignedLeft - alignedRight;

  const coefficient = difference * multiplierDecimal.coefficient;
  const combinedScale = differenceScale + multiplierDecimal.scale;

  if (combinedScale > MAX_DECIMAL_SCALE * 2) {
    throw new Error("Difference multiplication precision exceeds the supported range.");
  }

  return rescale(coefficient, combinedScale, targetScale);
}

export function divideToScaledInteger(
  numerator: number | string,
  denominator: number | string,
  targetScale: number,
): bigint {
  assertScale(targetScale);

  const numeratorDecimal = parseDecimal(numerator);
  const denominatorDecimal = parseDecimal(denominator);

  if (denominatorDecimal.coefficient === 0n) {
    throw new Error("Division by zero is not allowed.");
  }

  const negative = denominatorDecimal.coefficient < 0n;
  const signedNumerator = negative
    ? -numeratorDecimal.coefficient
    : numeratorDecimal.coefficient;

  const exponent =
    targetScale +
    denominatorDecimal.scale -
    numeratorDecimal.scale;

  let scaledNumerator = signedNumerator;
  let scaledDenominator = denominatorDecimal.coefficient < 0n
    ? -denominatorDecimal.coefficient
    : denominatorDecimal.coefficient;

  if (exponent >= 0) {
    scaledNumerator *= powerOfTen(exponent);
  } else {
    scaledDenominator *= powerOfTen(-exponent);
  }

  return divideRounded(scaledNumerator, scaledDenominator);
}

export function addToScaledInteger(
  left: number | string,
  right: number | string,
  targetScale: number,
): bigint {
  assertScale(targetScale);

  const leftDecimal = parseDecimal(left);
  const rightDecimal = parseDecimal(right);

  const [alignedLeft, alignedRight, commonScale] =
    alignDecimals(leftDecimal, rightDecimal);

  return rescale(
    alignedLeft + alignedRight,
    commonScale,
    targetScale,
  );
}

export function subtractToScaledInteger(
  left: number | string,
  right: number | string,
  targetScale: number,
): bigint {
  assertScale(targetScale);

  const leftDecimal = parseDecimal(left);
  const rightDecimal = parseDecimal(right);

  const [alignedLeft, alignedRight, commonScale] =
    alignDecimals(leftDecimal, rightDecimal);

  return rescale(
    alignedLeft - alignedRight,
    commonScale,
    targetScale,
  );
}
export function productToScaledInteger(
  values: (number | string)[],
  targetScale: number,
): bigint {
  assertScale(targetScale);

  if (!Array.isArray(values) || values.length === 0 || values.length > 10) {
    throw new Error("Product requires between 1 and 10 decimal factors.");
  }

  let coefficient = 1n;
  let combinedScale = 0;

  for (const value of values) {
    const decimal = parseDecimal(value);
    coefficient *= decimal.coefficient;
    combinedScale += decimal.scale;

    if (combinedScale > MAX_DECIMAL_SCALE * 2) {
      throw new Error("Product precision exceeds the supported range.");
    }
  }

  return rescale(coefficient, combinedScale, targetScale);
}

export function productDivideToScaledInteger(
  left: number | string,
  right: number | string,
  denominator: number | string,
  targetScale: number,
): bigint {
  assertScale(targetScale);

  const leftDecimal = parseDecimal(left);
  const rightDecimal = parseDecimal(right);
  const denominatorDecimal = parseDecimal(denominator);

  if (denominatorDecimal.coefficient === 0n) {
    throw new Error("Division by zero is not allowed.");
  }

  const productCoefficient =
    leftDecimal.coefficient * rightDecimal.coefficient;

  const productScale = leftDecimal.scale + rightDecimal.scale;

  if (productScale > MAX_DECIMAL_SCALE * 2) {
    throw new Error("Product precision exceeds the supported range.");
  }

  const exponent =
    targetScale + denominatorDecimal.scale - productScale;

  const negativeDenominator = denominatorDecimal.coefficient < 0n;

  let numerator = negativeDenominator
    ? -productCoefficient
    : productCoefficient;

  let divisor = negativeDenominator
    ? -denominatorDecimal.coefficient
    : denominatorDecimal.coefficient;

  if (exponent >= 0) {
    numerator *= powerOfTen(exponent);
  } else {
    divisor *= powerOfTen(-exponent);
  }

  return divideRounded(numerator, divisor);
}
export function scaledIntegerToNumber(
  value: bigint,
  scale: number,
): number {
  assertScale(scale);

  if (typeof value !== "bigint") {
    throw new Error("Scaled value must be a BigInt.");
  }

  if (
    value > BigInt(Number.MAX_SAFE_INTEGER) ||
    value < BigInt(Number.MIN_SAFE_INTEGER)
  ) {
    throw new Error("Scaled integer exceeds the safe precision limit.");
  }

  const result = Number(value) / Number(powerOfTen(scale));

  if (!Number.isFinite(result)) {
    throw new Error("Scaled value cannot be represented as a finite number.");
  }

  return result;
}