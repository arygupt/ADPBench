module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);
    localparam integer BEATS = (LEN + LANES - 1) / LANES;
    localparam integer CW = (BEATS < 2) ? 1 : $clog2(BEATS + 1);

    reg [LANES*DATA_W-1:0] a_buf, b_buf;
    reg have_a, have_b;
    reg [CW-1:0] a_count, b_count, pair_count;
    reg signed [ACC_W-1:0] accum;
    reg signed [ACC_W-1:0] result_reg;
    reg valid_reg;

    wire pair_fire = have_a && have_b;
    wire a_ready_int = !valid_reg && (a_count < BEATS) && (!have_a || pair_fire);
    wire b_ready_int = !valid_reg && (b_count < BEATS) && (!have_b || pair_fire);

    assign in_a_flat_ready = a_ready_int;
    assign in_b_flat_ready = b_ready_int;
    assign out_valid = valid_reg;
    assign out_c = result_reg;

    function automatic signed [ACC_W-1:0] dot_beat;
        input [LANES*DATA_W-1:0] aa;
        input [LANES*DATA_W-1:0] bb;
        integer k;
        reg signed [DATA_W-1:0] av;
        reg signed [DATA_W-1:0] bv;
        reg signed [2*DATA_W-1:0] product;
        reg signed [ACC_W-1:0] total;
        begin
            total = {ACC_W{1'b0}};
            for (k = 0; k < LANES; k = k + 1) begin
                av = aa[k*DATA_W +: DATA_W];
                bv = bb[k*DATA_W +: DATA_W];
                product = av * bv;
                total = total + product;
            end
            dot_beat = total;
        end
    endfunction

    wire signed [ACC_W-1:0] beat_dot = dot_beat(a_buf, b_buf);
    wire signed [ACC_W-1:0] next_accum = accum + beat_dot;

    always @(posedge clk) begin
        if (!rst_n) begin
            a_buf <= {LANES*DATA_W{1'b0}};
            b_buf <= {LANES*DATA_W{1'b0}};
            have_a <= 1'b0;
            have_b <= 1'b0;
            a_count <= {CW{1'b0}};
            b_count <= {CW{1'b0}};
            pair_count <= {CW{1'b0}};
            accum <= {ACC_W{1'b0}};
            result_reg <= {ACC_W{1'b0}};
            valid_reg <= 1'b0;
        end else if (valid_reg && out_ready) begin
            valid_reg <= 1'b0;
            accum <= {ACC_W{1'b0}};
            a_count <= {CW{1'b0}};
            b_count <= {CW{1'b0}};
            pair_count <= {CW{1'b0}};
            have_a <= 1'b0;
            have_b <= 1'b0;
        end else begin
            if (a_ready_int && in_a_flat_valid) begin
                a_buf <= in_a_flat;
                a_count <= a_count + 1'b1;
                have_a <= 1'b1;
            end else if (pair_fire) begin
                have_a <= 1'b0;
            end

            if (b_ready_int && in_b_flat_valid) begin
                b_buf <= in_b_flat;
                b_count <= b_count + 1'b1;
                have_b <= 1'b1;
            end else if (pair_fire) begin
                have_b <= 1'b0;
            end

            if (pair_fire) begin
                accum <= next_accum;
                pair_count <= pair_count + 1'b1;
                if (pair_count == BEATS - 1) begin
                    result_reg <= next_accum;
                    valid_reg <= 1'b1;
                end
            end
        end
    end
endmodule
